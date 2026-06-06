"""
test_providers.py - Validate LLM API keys and provider configurations.
This script bypasses the core application logic and uses the llm_client
directly to ping each configured provider.
"""

import argparse
import time

try:
    from . import config_engine
    from . import llm_client
except ImportError:
    import config_engine
    import llm_client

def main():
    parser = argparse.ArgumentParser(description="Test configured LLM providers and keys.")
    parser.add_argument("--all-models", action="store_true", help="Test every model for each key (default: stop after first success)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Sleep delay between requests to avoid rate limits (default: 1.0s)")
    args = parser.parse_args()

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
    provider_stats = {}

    for provider in providers:
        provider_stats[provider.name] = {"passed": 0, "failed": 0}
        print(f"\n--- Testing Provider: {provider.name} ---")
        
        valid_keys = [k for k in provider.api_keys if k and not k.startswith("gsk_xxx") and not k.startswith("AIzaSyXX") and not k.startswith("csk-xxx")]
        
        if not valid_keys:
            if provider.rpm_limit == 0:
                valid_keys = ["(local)"]
            else:
                print(f"  [SKIPPED] No valid API keys found for {provider.name}.")
                continue
                
        models_to_test = provider.models if provider.models else ["unknown"]

        for key in valid_keys:
            key_label = f"...{key[-4:]}" if key and key != "(local)" else "local"
            print(f"  Testing key {key_label}:")
            
            for model_to_test in models_to_test:
                total_keys += 1
                
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
                            print(f"    -> [SUCCESS] Model {model_to_test} is working perfectly!")
                            success_count += 1
                            provider_stats[provider.name]["passed"] += 1
                            if not args.all_models:
                                print("    -> (Stopping here for this key. Use --all-models to test the rest).")
                                break
                        else:
                            print(f"    -> [WARNING] Model {model_to_test} responded, but JSON parsing failed. Raw: {text[:50]}")
                            provider_stats[provider.name]["failed"] += 1
                    else:
                        print(f"    -> [FAIL] Model {model_to_test} failed to get a valid response.")
                        provider_stats[provider.name]["failed"] += 1
                        
                except Exception as e:
                    print(f"    -> [ERROR] Exception testing model {model_to_test}: {e}")
                    provider_stats[provider.name]["failed"] += 1
                
                time.sleep(args.sleep)

    print("\n" + "="*60)
    print(" Provider Breakdown:")
    print("-" * 60)
    for p_name, stats in provider_stats.items():
        total_p = stats['passed'] + stats['failed']
        status_icon = "✅" if stats['passed'] > 0 else "❌"
        if total_p == 0:
            print(f"  {p_name:<15} [SKIPPED] no tests run")
        else:
            print(f"  {p_name:<15} {status_icon} {stats['passed']}/{total_p} passed")
    print("-" * 60)
    print(f" Total Validation: {success_count}/{total_keys} tests passed.")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
