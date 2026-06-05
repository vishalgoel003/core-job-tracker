"""
llm_client.py — LLM API Client with Model-First Cascade & Per-Key Rate-Limit Awareness
----------------------------------------------------------------------------------------
Thin HTTP wrapper supporting cloud free-tier APIs (Groq, Gemini, Cerebras,
OpenRouter) and local endpoints (Ollama, LM Studio) via a unified
model-first cascade failover mechanism.

Key design decisions:
  - Model-first cascade: stages define an ordered list of preferred models.
    The client finds all providers capable of serving each model, then
    exhausts all API keys for those providers before falling back to the
    next model in the list.
  - Per-key rate-limit tracking: each (provider, api_key) pair has its own
    rate-limit state. One exhausted key does not block sibling keys.
  - Round-robin key exhaustion: all keys for all capable providers are tried
    before the cascade advances to the next model.
  - Auto-detect adapter type from base_url (openai/gemini). Cohere adapter retained but deprecated.
  - Zero SDK dependencies — pure requests [TECH-1.2], [TECH-1.5].

AGENT.md compliance:
  [TECH-1.2]  Pure requests library.
  [TECH-1.5]  HTTP calls to LLM endpoints authorized. No SDK dependencies.
  [NET-2.1]   Stateful requests.Session() for all LLM calls.
  [NET-2.2]   Browser User-Agent on every request.
  [NET-2.4]   Rate-limit header parsing and cascade on 429.
  [SEC-3.3]   API keys loaded from config.yaml at runtime, never hardcoded.
"""

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

try:
    from . import config_engine
except ImportError:
    import config_engine


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider endpoint."""
    name: str
    api_keys: list[str]     # Multiple keys for family accounts; [""] for local
    base_url: str
    models: list[str]       # Ordered list of models this provider can serve
    rpm_limit: int          # Per-key requests/minute limit; 0 = unlimited (local)
    adapter: str = ""       # "openai" | "gemini" | "cohere" — auto-detected


@dataclass
class StageParams:
    """Per-pipeline-stage parameters (temperature, max_tokens, model preference)."""
    temperature: float = 0.15
    max_tokens: int = 1500
    models: list[str] = field(default_factory=list)
    # Ordered list of preferred model names for this stage.
    # The client exhausts all keys for all providers capable of serving
    # each model before falling back to the next model in the list.
    # If empty, all available models across all providers are tried in order.


# ---------------------------------------------------------------------------
# Module-level rate-limit state (session lifetime)
# ---------------------------------------------------------------------------

_rate_limit_state: dict[str, dict[str, Any]] = {}
# Key format: "provider_name::key_suffix"  e.g. "groq::abc...xyz8"
# where key_suffix is the last 8 chars of the API key (safe for logging)


# ---------------------------------------------------------------------------
# Adapter auto-detection
# ---------------------------------------------------------------------------

def _detect_adapter(base_url: str) -> str:
    """
    Auto-detect the API adapter type from the base_url.
    - Contains 'generativelanguage.googleapis.com' → "gemini"
    - Contains 'cohere.com' → "cohere"
    - Everything else (Groq, Cerebras, OpenRouter, Ollama, LM Studio) → "openai"
    """
    url_lower = base_url.lower()
    if "generativelanguage.googleapis.com" in url_lower:
        return "gemini"
    if "cohere.com" in url_lower:
        return "cohere"
    return "openai"


def _rate_key(provider_name: str, api_key: str) -> str:
    """Build composite rate-limit state key from provider name and key suffix."""
    suffix = api_key[-8:] if len(api_key) >= 8 else api_key or "local"
    return f"{provider_name}::{suffix}"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_llm_config(config: dict) -> tuple[list[ProviderConfig], dict[str, StageParams]]:
    """
    Parse llm.providers and llm.stages from config dict.

    Provider config (new schema):
      api_keys: list of strings  — multiple accounts per provider
      models:   list of strings  — models this provider can serve

    Stage config (new schema):
      models: ordered list of preferred models for this stage

    Returns:
        (providers, stage_params_map)
        - providers: list of ProviderConfig, in config order
        - stage_params_map: dict mapping stage name → StageParams
    """
    llm_cfg = config.get("llm") or {}
    raw_providers = llm_cfg.get("providers") or []

    providers: list[ProviderConfig] = []
    for p in raw_providers:
        base_url = str(p.get("base_url") or "").strip()
        if not base_url:
            continue

        # Parse api_keys — list of strings; default to [""] for local endpoints
        raw_keys = p.get("api_keys") or []
        if isinstance(raw_keys, str):
            raw_keys = [raw_keys]
        api_keys: list[str] = [str(k).strip() for k in raw_keys if str(k).strip()]
        if not api_keys:
            api_keys = [""]  # local endpoint — no key needed

        # Parse models
        raw_models = p.get("models") or []
        if isinstance(raw_models, str):
            raw_models = [raw_models]
        models: list[str] = [str(m).strip() for m in raw_models if str(m).strip()]
        if not models:
            continue  # provider with no models configured is unusable

        rpm_limit = int(p.get("rpm_limit") or 0)

        # For cloud providers, skip if all keys look like placeholders
        if rpm_limit > 0:
            real_keys = [
                k for k in api_keys
                if k and not any(k.startswith(prefix) for prefix in
                                 ("gsk_xxx", "AIzaSyXX", "csk-xxx", "sk-xxx"))
            ]
            if not real_keys:
                continue
            api_keys = real_keys

        provider = ProviderConfig(
            name=str(p.get("name") or "unnamed"),
            api_keys=api_keys,
            base_url=base_url,
            models=models,
            rpm_limit=rpm_limit,
        )
        provider.adapter = _detect_adapter(base_url)
        providers.append(provider)

    # Parse stage params
    raw_stages = llm_cfg.get("stages") or {}
    stage_params_map: dict[str, StageParams] = {}
    for stage_name, stage_cfg in raw_stages.items():
        if not isinstance(stage_cfg, dict):
            continue
        raw_stage_models = stage_cfg.get("models") or []
        if isinstance(raw_stage_models, str):
            raw_stage_models = [raw_stage_models]
        stage_models = [str(m).strip() for m in raw_stage_models if str(m).strip()]

        params = StageParams(
            temperature=float(stage_cfg.get("temperature", 0.15)),
            max_tokens=int(stage_cfg.get("max_tokens", 1500)),
            models=stage_models,
        )
        stage_params_map[stage_name] = params

    return providers, stage_params_map


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------

def _update_rate_limit_state(rate_key: str, response: requests.Response) -> None:
    """Parse rate-limit headers from response and update per-key state."""
    state = _rate_limit_state.setdefault(rate_key, {})

    # Retry-After (seconds or HTTP-date)
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            state["retry_after_until"] = time.time() + float(retry_after)
        except ValueError:
            state["retry_after_until"] = None

    # X-RateLimit-Remaining — try both standard and Groq/Cerebras variants
    remaining = (response.headers.get("X-RateLimit-Remaining") or
                 response.headers.get("x-ratelimit-remaining") or
                 response.headers.get("x-ratelimit-remaining-requests"))
    if remaining is not None:
        try:
            state["remaining"] = int(remaining)
        except ValueError:
            pass

    # X-RateLimit-Reset — try both standard and Groq/Cerebras variants
    reset = (response.headers.get("X-RateLimit-Reset") or
             response.headers.get("x-ratelimit-reset") or
             response.headers.get("x-ratelimit-reset-requests"))
    if reset is not None:
        reset_str = reset.lower()
        if any(c in reset_str for c in ("m", "s", "h")):
            # Parse duration strings like "6m0s", "2.1s", "1h"
            total_s = 0.0
            for val, unit in re.findall(r'(\d+(?:\.\d+)?)([hms])', reset_str):
                val_f = float(val)
                if unit == 'h':
                    total_s += val_f * 3600
                elif unit == 'm':
                    total_s += val_f * 60
                elif unit == 's':
                    total_s += val_f
            if total_s > 0:
                state["reset_at"] = time.time() + total_s
        else:
            try:
                reset_val = float(reset)
                # If it looks like a unix timestamp (> year 2000 in seconds)
                state["reset_at"] = reset_val if reset_val > 946684800 else time.time() + reset_val
            except ValueError:
                pass


def _is_rate_limited(rate_key: str) -> bool:
    """Check if a provider+key is currently rate-limited based on observed headers."""
    state = _rate_limit_state.get(rate_key)
    if not state:
        return False

    now = time.time()

    # Check Retry-After
    retry_until = state.get("retry_after_until")
    if retry_until and now < retry_until:
        return True

    # Check remaining quota
    remaining = state.get("remaining")
    reset_at = state.get("reset_at")
    if remaining is not None and remaining <= 0:
        if reset_at and now < reset_at:
            return True
        else:
            # Reset window has passed, clear stale state
            state.pop("remaining", None)
            state.pop("reset_at", None)

    return False


# ---------------------------------------------------------------------------
# Provider-specific request builders (adapters)
# ---------------------------------------------------------------------------

def _build_openai_request(
    provider: ProviderConfig,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, dict, dict]:
    """
    Build request for OpenAI-compatible APIs.
    Works with: Groq, Cerebras, OpenRouter, Ollama, LM Studio.
    Returns: (url, headers, body)
    """
    url = provider.base_url

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        body["response_format"] = {"type": "json_object"}

    return url, headers, body


def _build_gemini_request(
    provider: ProviderConfig,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, dict, dict]:
    """
    Build request for Google Gemini API (generateContent endpoint).
    Returns: (url, headers, body)
    """
    base = provider.base_url.rstrip("/")
    url = f"{base}/{model}:generateContent?key={api_key}"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"

    return url, headers, body


def _build_cohere_request(
    provider: ProviderConfig,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, dict, dict]:
    """
    Build request for Cohere's /v2/chat API.
    Returns: (url, headers, body)
    """
    url = provider.base_url

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Cohere's command-a models have unstable support for response_format: {"type": "json_object"}
    # They return 500 errors or empty content when it is passed.
    # We rely on the system prompt to enforce JSON output.
    # if json_mode:
    #     body["response_format"] = {"type": "json_object"}

    return url, headers, body


# ---------------------------------------------------------------------------
# Response text extraction (adapter-specific)
# ---------------------------------------------------------------------------

def _extract_response_text(adapter: str, response_json: dict) -> str | None:
    """Extract the generated text from a provider-specific response JSON.

    Handles thinking/reasoning models:
      - Gemini: some models (e.g., gemma-4-31b-it) return multiple parts where
        the first part(s) are "thought" and the last part is the actual answer.
        We iterate parts in reverse and return the first non-empty text part,
        which is typically the final answer containing the JSON.
      - OpenAI: reasoning models (e.g., zai-glm-4.7 on Cerebras) may put
        output in message.reasoning when content is null (finish_reason=length).
    """
    result = None
    if adapter == "openai":
        choices = response_json.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            result = message.get("content")
            # Fallback for reasoning models where content is null but
            # reasoning contains the actual output (e.g., truncated by max_tokens)
            if not result and message.get("reasoning"):
                # Don't use raw reasoning as response — it's typically
                # incomplete chain-of-thought, not the final answer.
                # Log and return None so the cascade advances.
                finish = choices[0].get("finish_reason", "")
                if finish == "length":
                    print(f"  [LLM] Response truncated (finish_reason=length) — model used all tokens on reasoning.")
                pass

    elif adapter == "gemini":
        candidates = response_json.get("candidates") or []
        if candidates:
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            if parts:
                # Thinking models emit multiple parts: thought parts first,
                # then the actual answer last. Iterate in reverse to find
                # the actual text answer (skip empty parts).
                for part in reversed(parts):
                    text = part.get("text", "").strip()
                    if text:
                        result = text
                        break

    elif adapter == "cohere":
        message = response_json.get("message") or {}
        content = message.get("content") or []
        for block in content:
            if block.get("type") == "text":
                result = block.get("text")
                break

    if not result:
        print(f"  [DEBUG-EXTRACT] Failed to extract text for {adapter}. Raw JSON:")
        import json
        print(f"  {json.dumps(response_json)[:500]}")
        
    return result


# ---------------------------------------------------------------------------
# JSON extraction from LLM response text
# ---------------------------------------------------------------------------

def extract_json(raw_text: str) -> dict | None:
    """
    Robustly extract JSON from LLM response text.

    Handles:
      - Raw JSON string
      - Markdown-fenced JSON (```json ... ```)
      - Preamble/postamble text around JSON object

    Returns None on parse failure.
    """
    if not raw_text:
        return None

    text = raw_text.strip()

    # Attempt 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: strip markdown fences
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Attempt 3: extract first {...} block
    brace_match = re.search(r'\{[\s\S]*\}', text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Core LLM call with model-first cascade
# ---------------------------------------------------------------------------

def call_llm(
    providers: list[ProviderConfig],
    system_prompt: str,
    user_prompt: str,
    stage: str = "default",
    stage_params: StageParams | None = None,
    json_mode: bool = True,
    validator_fn: Callable[[str], bool] | None = None,
) -> tuple[str | None, ProviderConfig | None, str | None]:
    """
    Try LLM providers in model-first cascade order.
    Returns (response_text, used_provider, used_model) or (None, None, None) on total failure.

    Cascade algorithm:
      1. Determine ordered model list from stage_params.models.
         If empty, build from all providers in config order.
         If curated models don't exist in any provider, fall back to all available.
      2. For each model:
         a. Find all providers capable of serving it (provider.models contains model).
         b. For each capable provider, round-robin through ALL api_keys:
            - If key is rate-limited → skip to next key
            - On 429 → update per-key rate-limit state → next key
            - On success → return immediately
         c. If all keys for all capable providers exhausted → next model
      3. If all models exhausted → return (None, None), never crash.

    [NET-2.1] Uses requests.Session() for all attempts.
    [NET-2.4] Parses and respects rate-limit headers per key.
    """
    if not providers:
        print("  [LLM] No providers configured. Check config.yaml llm.providers section.")
        return None, None, None

    params = stage_params or StageParams()
    temperature = params.temperature
    max_tokens = params.max_tokens

    # Build the ordered model list
    requested_models = list(params.models)  # copy

    if requested_models:
        # Verify at least one curated model is available in some provider
        available = any(m in p.models for m in requested_models for p in providers)
        if not available:
            print(f"  [LLM] WARNING: No provider has any of {requested_models}. Falling back to all available models.")
            requested_models = []

    if not requested_models:
        # Fallback: gather all models from all providers, preserving config order
        seen: set[str] = set()
        for p in providers:
            for m in p.models:
                if m not in seen:
                    requested_models.append(m)
                    seen.add(m)

    session = requests.Session()

    for model in requested_models:
        # All providers capable of serving this model
        capable_providers = [p for p in providers if model in p.models]

        for provider in capable_providers:
            adapter = provider.adapter

            for api_key in provider.api_keys:
                rk = _rate_key(provider.name, api_key)

                # Skip rate-limited keys (cloud only)
                if provider.rpm_limit > 0 and _is_rate_limited(rk):
                    print(f"  [LLM] Skipping {provider.name} ({api_key[-4:]}...) — rate limited")
                    continue

                # Build adapter-specific request
                if adapter == "gemini":
                    url, headers, body = _build_gemini_request(
                        provider, api_key, model,
                        system_prompt, user_prompt, temperature, max_tokens, json_mode
                    )
                elif adapter == "cohere":
                    url, headers, body = _build_cohere_request(
                        provider, api_key, model,
                        system_prompt, user_prompt, temperature, max_tokens, json_mode
                    )
                else:
                    url, headers, body = _build_openai_request(
                        provider, api_key, model,
                        system_prompt, user_prompt, temperature, max_tokens, json_mode
                    )

                key_label = f"...{api_key[-4:]}" if api_key else "local"
                print(f"  [LLM] Trying {provider.name} ({model}) key={key_label} ...")

                try:
                    response = session.post(url, json=body, headers=headers, timeout=60)
                except requests.exceptions.RequestException as exc:
                    print(f"  [LLM] {provider.name} network error: {exc}")
                    continue

                # Update per-key rate-limit state from response headers
                if provider.rpm_limit > 0:
                    _update_rate_limit_state(rk, response)

                if response.status_code == 429:
                    print(f"  [LLM] {provider.name} key={key_label} 429 — trying next key.")
                    continue

                if response.status_code >= 500:
                    print(f"  [LLM] {provider.name} returned {response.status_code} (server error). Trying next.")
                    continue

                if response.status_code != 200:
                    # JSON-mode fallback: if provider rejects strict JSON mode
                    # (e.g., Groq 400 json_validate_failed), retry WITHOUT json_mode
                    # and rely on extract_json() to parse the free-text response.
                    if (
                        response.status_code == 400
                        and json_mode
                        and "json_validate_failed" in response.text[:500]
                    ):
                        print(f"  [LLM] {provider.name} strict JSON mode failed — retrying without json_mode ...")
                        if adapter == "gemini":
                            url2, headers2, body2 = _build_gemini_request(
                                provider, api_key, model,
                                system_prompt, user_prompt, temperature, max_tokens,
                                json_mode=False,
                            )
                        elif adapter == "cohere":
                            url2, headers2, body2 = _build_cohere_request(
                                provider, api_key, model,
                                system_prompt, user_prompt, temperature, max_tokens,
                                json_mode=False,
                            )
                        else:
                            url2, headers2, body2 = _build_openai_request(
                                provider, api_key, model,
                                system_prompt, user_prompt, temperature, max_tokens,
                                json_mode=False,
                            )

                        try:
                            retry_resp = session.post(url2, json=body2, headers=headers2, timeout=60)
                        except requests.exceptions.RequestException as exc:
                            print(f"  [LLM] {provider.name} retry network error: {exc}")
                            continue

                        # NOTE: Do NOT update rate-limit state here. The original
                        # 400 response already counted against the rate limit.
                        # Updating again would double-count and poison the tracker,
                        # causing all subsequent models on this provider to be skipped.

                        if retry_resp.status_code == 200:
                            try:
                                retry_json = retry_resp.json()
                            except ValueError:
                                print(f"  [LLM] {provider.name} retry returned non-JSON response")
                                continue

                            retry_text = _extract_response_text(adapter, retry_json)
                            if retry_text:
                                # Validate that the free-text response contains parseable JSON
                                # before accepting — otherwise we'd short-circuit the cascade
                                # with garbage text the caller can't use.
                                parsed_check = extract_json(retry_text)
                                if parsed_check is None:
                                    print(f"  [LLM] {provider.name} fallback response not parseable JSON ({len(retry_text)} chars). Continuing cascade.")
                                    continue

                                # Also run the caller's validator if provided
                                if validator_fn and not validator_fn(retry_text):
                                    print(f"  [LLM] {provider.name} fallback response failed validation. Continuing cascade.")
                                    continue

                                print(f"  [LLM] JSON-mode fallback success from {provider.name} ({model}) — {len(retry_text)} chars")
                                return retry_text, provider, model

                        print(f"  [LLM] {provider.name} retry also failed (HTTP {retry_resp.status_code}). Moving on.")
                        continue

                    print(f"  [LLM] {provider.name} returned HTTP {response.status_code}")
                    print(f"         Body (first 300 chars): {response.text[:300]}")
                    continue

                try:
                    response_json = response.json()
                except ValueError:
                    print(f"  [LLM] {provider.name} returned non-JSON response")
                    continue

                text = _extract_response_text(adapter, response_json)
                if not text:
                    print(f"  [LLM] {provider.name} returned empty content. Trying next.")
                    continue

                if validator_fn:
                    if not validator_fn(text):
                        print(f"  [LLM] {provider.name} response failed validation (lazy or invalid). Trying next.")
                        continue

                print(f"  [LLM] Success from {provider.name} ({model}) — {len(text)} chars")
                return text, provider, model

    print("  [LLM] All models × providers × keys exhausted. No response obtained.")
    return None, None, None


# ---------------------------------------------------------------------------
# Standalone test entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Smoke test: load config, try a trivial prompt through the cascade."""
    print()
    print("=== llm_client.py — LLM Client Smoke Test ===")
    print()

    config = config_engine.load_config("config.yaml")
    providers, stage_params = load_llm_config(config)

    if not providers:
        print("[ERROR] No usable LLM providers found in config.yaml.")
        print("        Ensure at least one provider has a valid api_key (or is a local endpoint).")
        sys.exit(1)

    print(f"[OK] {len(providers)} provider(s) loaded:")
    for p in providers:
        print(f"     - {p.name}: {len(p.api_keys)} key(s), models={p.models}")
    print(f"[OK] {len(stage_params)} stage config(s): {list(stage_params.keys())}")
    for sn, sp in stage_params.items():
        print(f"     - {sn}: models={sp.models}, temp={sp.temperature}, max_tokens={sp.max_tokens}")
    print()

    # Try a trivial JSON extraction prompt
    test_prompt = 'Respond with exactly this JSON: {"status": "ok", "provider": "your_name"}'
    text, used_provider, used_model = call_llm(
        providers,
        system_prompt="You are a test assistant. Return only valid JSON.",
        user_prompt=test_prompt,
        stage="default",
        json_mode=True,
    )

    if text:
        print(f"\n[OK] Raw response: {text[:200]}")
        parsed = extract_json(text)
        if parsed:
            print(f"[OK] Parsed JSON: {json.dumps(parsed, indent=2)}")
        else:
            print("[WARN] Could not parse JSON from response.")
    else:
        print("\n[FAIL] No response from any provider.")

    print()
    print("[DONE] Smoke test complete.")
    print()


if __name__ == "__main__":
    main()
