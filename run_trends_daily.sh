#!/bin/bash
# Daily trend sweep — run by cron. Logs to logs/trends_cron_<date>.log.
cd /Users/komailaltaf/Downloads/community-mapper || exit 1
mkdir -p logs
echo "=== trends run started $(date) ===" >> "logs/trends_cron.log"
/Users/komailaltaf/Downloads/community-mapper/.venv/bin/python -u scrape_trends.py >> "logs/trends_cron.log" 2>&1
echo "=== trends run finished $(date) ===" >> "logs/trends_cron.log"
