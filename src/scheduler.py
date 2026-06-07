import schedule
import time
import subprocess
import sys
import os
import logging

import config_engine

# Setup logger to echo to stdout so Docker captures it
logger = logging.getLogger("scheduler")
logger.setLevel(logging.INFO)
fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(fmt)
logger.addHandler(ch)

def run_scraper():
    logger.info("⏰ Triggering automated background scraper...")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    
    # We let state_tracker.py write to logs/scraper.log on its own.
    # We also capture its stdout to push to our own logger so it appears in docker logs.
    try:
        result = subprocess.run(
            [sys.executable, "src/state_tracker.py"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8"
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                logger.info(line)
        if result.stderr:
            for line in result.stderr.splitlines():
                logger.error(line)
        logger.info("✅ Automated background scrape complete.")
    except Exception as e:
        logger.error(f"❌ Failed to run scraper: {e}")

def main():
    try:
        config = config_engine.load_config("config.yaml")
    except Exception as e:
        logger.error(f"Failed to load config.yaml: {e}")
        return

    interval_hours = config.get("global_settings", {}).get("scraper_interval_hours", 12)
    
    logger.info(f"🚀 Scheduler started. Configured to run every {interval_hours} hours.")
    
    schedule.every(interval_hours).hours.do(run_scraper)
    
    # Run once immediately on startup
    run_scraper()
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
