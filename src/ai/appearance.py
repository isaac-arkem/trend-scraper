"""Two-stage appearance analysis with a hard, run-wide budget.

The old path sent every image to gpt-4o and asked it everything — including
"is there even a person here?", which is the cheapest question in the set and
was being answered at the most expensive price. In hashtag discovery most
images are product shots, text cards and scenery, so the majority of that spend
bought the answer "nobody visible".

    stage 1  gpt-4o-mini   person? child? ad?            ~$0.0002 / image
    stage 2  gpt-4o        the full appearance read      ~$0.004  / image

Only images with a clearly visible adult reach stage 2. That makes the cost
scale with useful images rather than with images fetched, and it is also how
children are kept out: a child is rejected at the gate and its bytes are never
sent to the second model, never written to appearance, never scored. That is a
safeguarding rule, not an optimisation, so it is enforced here in code rather
than by asking the second model to behave.

BUDGET
------
The cap is for the whole run, across every country in it, and the run's scrape
finishes before any of this starts — so the creator count is known before a
single cent is spent. plan_images_per_creator() divides the budget by that count
up front: a 3-country run with few creators gets the full 3 images each, a
6-country run with hundreds drops to 1. A live ledger then enforces the cap for
real, because per-image cost is an estimate and estimates drift.
"""
import base64
import json
import os
import threading
from typing import Optional

from src.ai.prompts import VISION_SYSTEM, VISION_USER
from src.ai.vision import (QuotaExceededError, _compress_image, _create_with_retry,
                           _parse_json, normalise_result)
from src.utils.logger import get_logger

log = get_logger(__name__)

GATE_MODEL = os.environ.get("VISION_GATE_MODEL", "gpt-4o-mini")
FULL_MODEL = os.environ.get("VISION_FULL_MODEL", "gpt-4o")

# USD per 1M tokens, as billed. Used to price each call from its ACTUAL token
# usage — never to guess after the fact.
PRICING = {
    "gpt-4o":      {"in": 2.50, "out": 10.00},
    "gpt-4o-mini": {"in": 0.15, "out":  0.60},
}

# Rough per-image cost, used only to divide the budget up front. Derived from
# the token shape of these two prompts at detail=low (an image is ~85 tokens).
EST_GATE_USD = 0.0002
EST_FULL_USD = 0.0040

# Per-creator limits. Ported from stage5_analysis.py, where they were measured:
# a creator whose feed is products or scenery used to burn the full cap before
# stopping, and dead creators are common in hashtag discovery.
MAX_IMAGES_PER_CREATOR = 3
GOOD_READS_TO_STOP     = 2   # enough usable reads — stop, the rest add nothing
ABANDON_AFTER          = 3   # nobody visible in the first N — this creator is dead

GATE_SYSTEM = """You are a triage assistant for a social media research tool.
You answer only whether an image is worth analysing further.
Never identify individuals. Never infer race, ethnicity, religion, nationality or any protected attribute.
Return only valid JSON."""

GATE_USER = """Look at this public social media image and return ONLY this JSON:

{"person_visible": true|false,
 "is_child": true|false,
 "is_ad_or_product": true|false,
 "confidence": 0.0-1.0}

Rules:
- person_visible: true only if a person is clearly visible enough to describe their appearance.
- is_child: true if the main subject appears to be a child or minor. When in any doubt, say true.
- is_ad_or_product: true if the image is mainly a product, graphic or text card rather than a person.
- Answer only these four fields. Nothing else."""


class Budget:
    """A hard spending ceiling for one run, shared across threads.

    charge() is called with the real cost after each call, and reserve() is
    asked BEFORE each call — so the cap holds even if the per-image estimate is
    wrong, which it will be for unusual images.
    """

    def __init__(self, cap_usd: float):
        self.cap = float(cap_usd)
        self.spent = 0.0
        self._lock = threading.Lock()
        self.stopped_early = False

    def can_afford(self, estimate: float) -> bool:
        with self._lock:
            return (self.spent + estimate) <= self.cap

    def charge(self, usd: float) -> None:
        with self._lock:
            self.spent += usd

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.cap - self.spent)

    def mark_stopped(self) -> None:
        with self._lock:
            self.stopped_early = True


def eur_to_usd(eur: float) -> float:
    """The cap is quoted to the client in euros; OpenAI bills in dollars."""
    return eur * float(os.environ.get("EUR_USD_RATE", "1.08"))


def _price(model: str, usage) -> float:
    p = PRICING.get(model)
    if not p or usage is None:
        return 0.0
    return ((getattr(usage, "prompt_tokens", 0) or 0) / 1e6) * p["in"] + \
           ((getattr(usage, "completion_tokens", 0) or 0) / 1e6) * p["out"]


def plan_images_per_creator(n_creators: int, budget: Budget,
                            max_per_creator: int = MAX_IMAGES_PER_CREATOR) -> int:
    """How many images each creator gets, so the whole run fits the cap.

    Assumes the worst realistic case — every gated image passes and costs a full
    read — because underspending is recoverable and overspending is not.
    """
    if n_creators <= 0:
        return max_per_creator
    per_image = EST_GATE_USD + EST_FULL_USD
    affordable = int(budget.remaining() // (n_creators * per_image))
    return max(1, min(max_per_creator, affordable))


def gate(image_bytes: bytes, budget: Budget) -> Optional[dict]:
    """Cheap triage. None means the call could not be made (budget or error)."""
    if not budget.can_afford(EST_GATE_USD):
        budget.mark_stopped()
        return None
    try:
        small, mime = _compress_image(image_bytes, max_kb=200)
        data_url = f"data:{mime};base64,{base64.b64encode(small).decode()}"
        resp = _create_with_retry(
            model=GATE_MODEL,
            messages=[
                {"role": "system", "content": GATE_SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": GATE_USER},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                ]},
            ],
            max_tokens=100,
            temperature=0,
        )
        budget.charge(_price(GATE_MODEL, resp.usage))
        return _parse_json(resp.choices[0].message.content.strip())
    except QuotaExceededError:
        raise
    except Exception as e:
        log.warning(f"gate failed: {str(e)[:90]}")
        return None


def full_read(image_bytes: bytes, budget: Budget) -> Optional[dict]:
    """The expensive read — only ever called on an image the gate approved."""
    if not budget.can_afford(EST_FULL_USD):
        budget.mark_stopped()
        return None
    for max_kb in (800, 400, 200):
        try:
            small, mime = _compress_image(image_bytes, max_kb=max_kb)
            data_url = f"data:{mime};base64,{base64.b64encode(small).decode()}"
            resp = _create_with_retry(
                model=FULL_MODEL,
                messages=[
                    {"role": "system", "content": VISION_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": VISION_USER},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ]},
                ],
                max_tokens=512,
                temperature=0,
            )
            budget.charge(_price(FULL_MODEL, resp.usage))
            return normalise_result(_parse_json(resp.choices[0].message.content.strip()))
        except QuotaExceededError:
            raise
        except Exception as e:
            if "431" in str(e):
                continue
            log.warning(f"full read failed: {str(e)[:90]}")
            return None
    return None


def analyse_creator(images: list, budget: Budget, per_creator: int) -> list:
    """Run both stages over ONE creator's images, in order, stopping early.

    `images` is [(clip_id, bytes), ...]. Returns one verdict dict per image
    actually looked at — callers write these to the clip rows. Images never
    reached (early stop, budget, abandoned creator) simply aren't in the list,
    so nothing is recorded as analysed that wasn't.
    """
    out = []
    good = 0
    blanks = 0
    for clip_id, data in images[:per_creator]:
        if good >= GOOD_READS_TO_STOP:
            break
        if blanks >= ABANDON_AFTER:
            break                      # products/scenery — the rest won't differ

        # Budget and failure are different endings and must not be conflated: out
        # of money means stop the whole run, a failed call means skip this image
        # and carry on. Recording an API error as "budget_exhausted" would have
        # made a broken key look like a spending cap being hit.
        if not budget.can_afford(EST_GATE_USD + EST_FULL_USD):
            budget.mark_stopped()
            out.append({"clip_id": clip_id, "stage": "budget_exhausted"})
            break
        g = gate(data, budget)
        if g is None:
            out.append({"clip_id": clip_id, "stage": "error"})
            continue

        # Safeguarding first, before anything else is considered. A child is
        # dropped here and its image is never sent to the second model.
        if g.get("is_child"):
            out.append({"clip_id": clip_id, "stage": "skipped_child"})
            continue
        if g.get("is_ad_or_product"):
            out.append({"clip_id": clip_id, "stage": "skipped_ad"})
            blanks += 1
            continue
        if not g.get("person_visible"):
            out.append({"clip_id": clip_id, "stage": "skipped_no_person"})
            blanks += 1
            continue

        r = full_read(data, budget)
        if r is None:
            out.append({"clip_id": clip_id, "stage": "error"})
            continue
        # The second model gets the final say on age — the gate is small and
        # cheap, so a child it let through must still not be recorded.
        if r.get("is_child"):
            out.append({"clip_id": clip_id, "stage": "skipped_child"})
            continue
        out.append({"clip_id": clip_id, "stage": "analysed", "appearance": r})
        if r.get("person_visible"):
            good += 1
    return out
