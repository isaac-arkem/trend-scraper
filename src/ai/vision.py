import os
import json
import base64
import httpx
from openai import OpenAI
from dotenv import load_dotenv
from src.ai.prompts import VISION_SYSTEM, VISION_USER
from src.utils.logger import get_logger

load_dotenv()
log = get_logger(__name__)

_client: OpenAI | None = None


def get_openai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


def analyze_image_url(image_url: str) -> dict | None:
    """Analyze an image from a public URL using GPT-4o Vision."""
    try:
        resp = get_openai().chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": VISION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_USER},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                    ],
                },
            ],
            max_tokens=512,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        return _parse_json(raw)
    except Exception as e:
        log.warning(f"Vision API failed for URL {image_url[:60]}: {e}")
        return None


def analyze_image_bytes(image_bytes: bytes, mime: str = "image/jpeg") -> dict | None:
    """Analyze an image from raw bytes (e.g. extracted video frame)."""
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"
        resp = get_openai().chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": VISION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_USER},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                },
            ],
            max_tokens=512,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        return _parse_json(raw)
    except Exception as e:
        log.warning(f"Vision API failed for bytes input: {e}")
        return None


def _parse_json(raw: str) -> dict | None:
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse vision response JSON: {e}\nRaw: {raw[:200]}")
        return None
