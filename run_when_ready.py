#!/usr/bin/env python3
"""
Wait for the OpenAI quota to come back, then auto-launch the capped + early-stop
Stage 5 run (gpt-4o). Polls a tiny chat call every 2 min — costs nothing while the
key is out of quota, fires the full run the instant it works.
Run:  .venv/bin/python run_when_ready.py
"""
import os
import time
import logging
from dotenv import load_dotenv

load_dotenv()
for _n in ("httpx", "httpcore", "openai", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from openai import OpenAI
import run_stage5_parallel as driver


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def quota_live() -> bool:
    try:
        OpenAI(api_key=os.environ["OPENAI_API_KEY"]).chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "ok"}],
            max_tokens=1, temperature=0)
        return True
    except Exception as e:
        if "insufficient_quota" in str(e):
            return False
        # Any other error (network blip etc.) — treat as not-ready, keep waiting.
        log(f"key check error (will retry): {str(e)[:90]}")
        return False


def main():
    log("Watching OpenAI quota — will auto-start Stage 5 (gpt-4o) when it clears.")
    waits = 0
    while not quota_live():
        waits += 1
        if waits % 5 == 0:
            log(f"still out of quota after {waits*2} min — waiting…")
        time.sleep(120)
    log("Quota is LIVE — launching Stage 5 run now.")
    driver.main()


if __name__ == "__main__":
    main()
