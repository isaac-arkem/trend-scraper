from typing import Union, Optional
import os
import time
from apify_client import ApifyClient
from dotenv import load_dotenv
from src.utils.logger import get_logger

load_dotenv()
log = get_logger(__name__)

_client: Optional[ApifyClient] = None


def get_apify() -> ApifyClient:
    global _client
    if _client is None:
        token = os.environ["APIFY_TOKEN"]
        _client = ApifyClient(token)
    return _client


def run_actor(actor_id: str, run_input: dict, max_items: int = None, label: str = "") -> list:
    """Run an Apify actor and return all dataset items."""
    client = get_apify()
    label_str = f"[{label}] " if label else ""

    log.info(f"{label_str}Running actor {actor_id} — input: {_summarise(run_input)}")

    try:
        run = client.actor(actor_id).call(run_input=run_input)
    except Exception as e:
        log.error(f"{label_str}Actor call failed: {e}")
        return []

    # Apify client returns a Run object in newer versions
    dataset_id = getattr(run, "default_dataset_id", None) or (run.get("defaultDatasetId") if isinstance(run, dict) else None)
    if not dataset_id:
        log.warning(f"{label_str}No dataset returned")
        return []

    items = list(client.dataset(dataset_id).iterate_items())

    if max_items and len(items) > max_items:
        items = items[:max_items]

    stats = getattr(run, "stats", None) or (run.get("stats", {}) if isinstance(run, dict) else {})
    compute_units = (stats.get("computeUnits", 0) if isinstance(stats, dict) else getattr(stats, "compute_units", 0)) or 0
    log.info(f"{label_str}Got {len(items)} items — {compute_units:.3f} CU used")

    return items


def _summarise(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, list):
            parts.append(f"{k}=[{len(v)} items]")
        else:
            parts.append(f"{k}={str(v)[:40]}")
    return ", ".join(parts)
