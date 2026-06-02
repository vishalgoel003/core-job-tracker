"""
llm_client.py — LLM API Client with Provider Cascade & Rate-Limit Awareness
-----------------------------------------------------------------------------
Thin HTTP wrapper supporting cloud free-tier APIs (Groq, Gemini, Cerebras,
Cohere, OpenRouter) and local endpoints (Ollama, LM Studio) via a unified
cascade failover mechanism.

Key design decisions:
  - Provider cascade: try providers in config order; on 429/5xx/timeout → next.
  - Per-stage parameters: temperature, max_tokens, and optional provider pinning.
  - Smart rate-limit awareness: parses Retry-After and X-RateLimit-* headers.
  - Local models (rpm_limit=0): skip all throttling.
  - Auto-detect adapter type from base_url (openai/gemini/cohere).
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
from typing import Any

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
    api_key: str            # Empty string for local endpoints
    base_url: str
    model: str
    rpm_limit: int          # 0 = unlimited (local models)
    adapter: str = ""       # "openai" | "gemini" | "cohere" — auto-detected


@dataclass
class StageParams:
    """Per-pipeline-stage parameters (temperature, max_tokens, overrides)."""
    temperature: float = 0.15
    max_tokens: int = 1500
    provider_override: str | None = None    # Pin to a specific provider name
    model_override: str | None = None       # Override provider's default model


# ---------------------------------------------------------------------------
# Module-level rate-limit state (session lifetime)
# ---------------------------------------------------------------------------

_rate_limit_state: dict[str, dict[str, Any]] = {}
# e.g. {"groq": {"remaining": 5, "reset_at": 1717350000.0, "retry_after": None}}


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


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_llm_config(config: dict) -> tuple[list[ProviderConfig], dict[str, StageParams]]:
    """
    Parse llm.providers and llm.stages from config dict.

    Returns:
        (providers, stage_params_map)
        - providers: list of ProviderConfig, in cascade order
        - stage_params_map: dict mapping stage name → StageParams
    """
    llm_cfg = config.get("llm") or {}
    raw_providers = llm_cfg.get("providers") or []

    providers: list[ProviderConfig] = []
    for p in raw_providers:
        api_key = str(p.get("api_key") or "").strip()
        base_url = str(p.get("base_url") or "").strip()

        # Skip providers with no base_url
        if not base_url:
            continue

        # Skip cloud providers with empty API key (local providers are fine)
        is_local = base_url.startswith("http://localhost") or base_url.startswith("http://127.0.0.1") or ":" in base_url.replace("https://", "").replace("http://", "").split("/")[0]
        # Actually, better heuristic: rpm_limit=0 or explicitly no api_key needed
        rpm_limit = int(p.get("rpm_limit") or 0)

        # For cloud providers, skip if api_key looks like a placeholder
        if rpm_limit > 0 and (not api_key or api_key.startswith("gsk_xxx") or api_key.startswith("AIzaSyXX") or api_key.startswith("csk-xxx")):
            continue

        provider = ProviderConfig(
            name=str(p.get("name") or "unnamed"),
            api_key=api_key,
            base_url=base_url,
            model=str(p.get("model") or ""),
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
        params = StageParams(
            temperature=float(stage_cfg.get("temperature", 0.15)),
            max_tokens=int(stage_cfg.get("max_tokens", 1500)),
            provider_override=stage_cfg.get("provider_override") or None,
            model_override=stage_cfg.get("model_override") or None,
        )
        stage_params_map[stage_name] = params

    return providers, stage_params_map


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------

def _update_rate_limit_state(provider_name: str, response: requests.Response) -> None:
    """Parse rate-limit headers from response and update module-level state."""
    state = _rate_limit_state.setdefault(provider_name, {})

    # Retry-After (seconds or HTTP-date)
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            state["retry_after_until"] = time.time() + float(retry_after)
        except ValueError:
            state["retry_after_until"] = None

    # X-RateLimit-Remaining
    remaining = response.headers.get("X-RateLimit-Remaining") or response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            state["remaining"] = int(remaining)
        except ValueError:
            pass

    # X-RateLimit-Reset (unix timestamp or seconds)
    reset = response.headers.get("X-RateLimit-Reset") or response.headers.get("x-ratelimit-reset")
    if reset is not None:
        try:
            reset_val = float(reset)
            # If it looks like a unix timestamp (> year 2000 in seconds)
            if reset_val > 946684800:
                state["reset_at"] = reset_val
            else:
                state["reset_at"] = time.time() + reset_val
        except ValueError:
            pass


def _is_rate_limited(provider_name: str) -> bool:
    """Check if a provider is currently rate-limited based on observed headers."""
    state = _rate_limit_state.get(provider_name)
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
            # Reset window has passed, clear state
            state.pop("remaining", None)
            state.pop("reset_at", None)

    return False


# ---------------------------------------------------------------------------
# Provider-specific request builders (adapters)
# ---------------------------------------------------------------------------

def _build_openai_request(
    provider: ProviderConfig,
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

    # Auth header handling
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    elif "lmstudio" in provider.name.lower():
        headers["Authorization"] = "Bearer not-needed"
    # Ollama with empty key: omit Authorization header entirely

    body: dict[str, Any] = {
        "model": provider.model,
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
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> tuple[str, dict, dict]:
    """
    Build request for Google Gemini's generateContent API.
    Returns: (url, headers, body)
    """
    model = provider.model
    url = f"{provider.base_url.rstrip('/')}/{model}:generateContent?key={provider.api_key}"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    body: dict[str, Any] = {
        "contents": [
            {
                "parts": [
                    {"text": f"{system_prompt}\n\n{user_prompt}"}
                ]
            }
        ],
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
        "Authorization": f"Bearer {provider.api_key}",
        "User-Agent": USER_AGENT,
    }

    body: dict[str, Any] = {
        "model": provider.model,
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


# ---------------------------------------------------------------------------
# Response text extraction (adapter-specific)
# ---------------------------------------------------------------------------

def _extract_response_text(adapter: str, response_json: dict) -> str | None:
    """Extract the generated text from a provider-specific response JSON."""
    if adapter == "openai":
        choices = response_json.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return message.get("content")
        return None

    elif adapter == "gemini":
        candidates = response_json.get("candidates") or []
        if candidates:
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            if parts:
                return parts[0].get("text")
        return None

    elif adapter == "cohere":
        message = response_json.get("message") or {}
        content = message.get("content") or []
        if content:
            return content[0].get("text")
        return None

    return None


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

    # Attempt 2: extract from markdown fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Attempt 3: find first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Core LLM call with cascade
# ---------------------------------------------------------------------------

def call_llm(
    providers: list[ProviderConfig],
    system_prompt: str,
    user_prompt: str,
    stage: str = "default",
    stage_params: StageParams | None = None,
    json_mode: bool = True,
) -> tuple[str | None, ProviderConfig | None]:
    """
    Try LLM providers in cascade order. Returns (response_text, used_provider).

    If stage_params.provider_override is set, skip straight to that provider.
    On 429/5xx/timeout → cascade to next provider.
    If all exhausted → return (None, None), never crash.

    [NET-2.1] Uses requests.Session() for each attempt.
    [NET-2.4] Parses and respects rate-limit headers.
    """
    if not providers:
        print("  [LLM] No providers configured. Check config.yaml llm.providers section.")
        return None, None

    params = stage_params or StageParams()
    temperature = params.temperature
    max_tokens = params.max_tokens

    # Determine effective provider list
    effective_providers = providers
    if params.provider_override:
        pinned = [p for p in providers if p.name == params.provider_override]
        if pinned:
            effective_providers = pinned
        else:
            print(f"  [LLM] WARNING: provider_override '{params.provider_override}' not found. Using full cascade.")

    session = requests.Session()
    llm_config_section = {}  # for inter_call_delay_s — loaded at call site

    for provider in effective_providers:
        model = params.model_override or provider.model

        # Skip rate-limited providers (cloud only)
        if provider.rpm_limit > 0 and _is_rate_limited(provider.name):
            print(f"  [LLM] Skipping {provider.name} — rate limited")
            continue

        # Build adapter-specific request
        adapter = provider.adapter
        if adapter == "gemini":
            url, headers, body = _build_gemini_request(
                provider, system_prompt, user_prompt, temperature, max_tokens, json_mode
            )
        elif adapter == "cohere":
            url, headers, body = _build_cohere_request(
                provider, system_prompt, user_prompt, temperature, max_tokens, json_mode
            )
        else:
            url, headers, body = _build_openai_request(
                provider, system_prompt, user_prompt, temperature, max_tokens, json_mode
            )

        print(f"  [LLM] Trying {provider.name} ({model}) ...")

        try:
            response = session.post(url, json=body, headers=headers, timeout=60)
        except requests.exceptions.RequestException as exc:
            print(f"  [LLM] {provider.name} network error: {exc}")
            continue

        # Update rate-limit state from response headers
        if provider.rpm_limit > 0:
            _update_rate_limit_state(provider.name, response)

        if response.status_code == 429:
            print(f"  [LLM] {provider.name} returned 429 (rate limited). Cascading to next.")
            continue

        if response.status_code >= 500:
            print(f"  [LLM] {provider.name} returned {response.status_code} (server error). Cascading.")
            continue

        if response.status_code != 200:
            print(f"  [LLM] {provider.name} returned HTTP {response.status_code}")
            print(f"         Body (first 300 chars): {response.text[:300]}")
            continue

        try:
            response_json = response.json()
        except ValueError:
            print(f"  [LLM] {provider.name} returned non-JSON response")
            continue

        text = _extract_response_text(adapter, response_json)
        if text:
            print(f"  [LLM] Success from {provider.name} ({len(text)} chars)")
            return text, provider
        else:
            print(f"  [LLM] {provider.name} returned empty content. Cascading.")
            continue

    print("  [LLM] All providers exhausted. No response obtained.")
    return None, None


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

    print(f"[OK] {len(providers)} provider(s) loaded: {[p.name for p in providers]}")
    print(f"[OK] {len(stage_params)} stage config(s): {list(stage_params.keys())}")
    print()

    # Try a trivial JSON extraction prompt
    test_prompt = 'Respond with exactly this JSON: {"status": "ok", "provider": "your_name"}'
    text, used = call_llm(
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
