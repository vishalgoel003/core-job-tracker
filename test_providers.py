"""
test_providers.py - Validate LLM API keys and provider configurations.
This script bypasses the core application logic and uses the llm_client
directly to ping each configured provider.
"""

import sys
import json
from src import config_engine, llm_client

def main():
    print("\n" + "="*60)
    print(" LLM Provider Key Validation Script")
    print("="*60 + "\n")

    try:
        config = config_engine.load_config("config.yaml")
        providers, _ = llm_client.load_llm_config(config)
    except Exception as e:
        print(f"[FAIL] Could not load config.yaml: {e}")
        return

    if not providers:
        print("[FAIL] No providers found in config.yaml.")
        return

    test_prompt = 'Respond with exactly this JSON: {"status": "ok"}'
    sys_prompt = "You are a test assistant. Return only valid JSON."

    total_keys = 0
    success_count = 0

    for provider in providers:
        print(f"\n--- Testing Provider: {provider.name} ---")
        
        valid_keys = [k for k in provider.api_keys if k and not k.startswith("gsk_xxx") and not k.startswith("AIzaSyXX") and not k.startswith("csk-xxx")]
        
        if not valid_keys:
            if provider.rpm_limit == 0:
                valid_keys = ["(local)"]
            else:
                print(f"  [SKIPPED] No valid API keys found for {provider.name}.")
                continue
                
        model_to_test = provider.models[0] if provider.models else "unknown"
        print(f"  Using model: {model_to_test}")

        for key in valid_keys:
            total_keys += 1
            key_label = f"...{key[-4:]}" if key and key != "(local)" else "local"
            print(f"  Testing key {key_label}...")
            
            # Create a temporary single-provider, single-key config to isolate the test
            temp_provider = llm_client.ProviderConfig(
                name=provider.name,
                api_keys=[key if key != "(local)" else ""],
                base_url=provider.base_url,
                models=[model_to_test],
                rpm_limit=provider.rpm_limit
            )
            temp_provider.adapter = provider.adapter
            
            try:
                # Silence the internal verbose logging of llm_client for cleaner test output
                text, used = llm_client.call_llm(
                    providers=[temp_provider],
                    system_prompt=sys_prompt,
                    user_prompt=test_prompt,
                    stage_params=llm_client.StageParams(models=[model_to_test]),
                    json_mode=True
                )
                
                if text:
                    parsed = llm_client.extract_json(text)
                    if parsed and parsed.get("status") == "ok":
                        print(f"  -> [SUCCESS] Key {key_label} is working perfectly!")
                        success_count += 1
                    else:
                        print(f"  -> [WARNING] Key {key_label} responded, but JSON parsing failed. Raw response: {text[:50]}")
                else:
                    print(f"  -> [FAIL] Key {key_label} failed to get a valid response.")
                    
            except Exception as e:
                print(f"  -> [ERROR] Exception testing key {key_label}: {e}")

    print("\n" + "="*60)
    print(f" Validation Complete: {success_count}/{total_keys} keys working.")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
