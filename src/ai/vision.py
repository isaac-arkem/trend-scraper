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
        return normalise_result(_parse_json(raw))
    except Exception as e:
        log.warning(f"Vision API failed for URL {image_url[:60]}: {e}")
        return None


VALID_VALUES = {
    "body_frame": {"petite","slim","average","curvy","athletic","plus","unclear"},
    "body_shape": {"pear","balanced","apple","unclear"},
    "skin_tone": {"porcelain","fair","light","medium","olive","golden-tan","tan","caramel","deep","dark","unclear"},
    "eye_color": {"brown","black","blue","green","hazel","unclear"},
    "hair_color": {"black","brown","blonde","red","dyed","mixed","covered","unclear"},
    "hair_length": {"short","medium","long","covered","unclear"},
    "hair_texture": {"straight","wavy","curly","coily","covered","unclear"},
    "makeup_style": {"natural","soft_glam","full_glam","bold","none_visible","unclear"},
    "image_quality": {"good","medium","poor"},
}

def normalise_result(result: dict) -> dict:
    """Map any AI-returned values not in the constraint lists to 'unclear'."""
    if not result:
        return result
    for field, valid in VALID_VALUES.items():
        v = result.get(field)
        if v and v not in valid:
            result[field] = "unclear"
    return result


def _compress_image(image_bytes: bytes, max_kb: int = 800) -> tuple[bytes, str]:
    """Compress image to under max_kb KB. Returns (bytes, mime_type)."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        # Resize if too large
        max_dim = 1024
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        # Compress
        out = io.BytesIO()
        quality = 85
        while quality >= 40:
            out.seek(0); out.truncate()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            if out.tell() <= max_kb * 1024:
                break
            quality -= 15
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/jpeg"


def analyze_image_bytes(image_bytes: bytes, mime: str = "image/jpeg") -> dict | None:
    """Analyze an image from raw bytes. Retries with smaller size on 431."""
    for max_kb in [800, 400, 200]:
        try:
            image_bytes_c, mime = _compress_image(image_bytes, max_kb=max_kb)
            b64 = base64.b64encode(image_bytes_c).decode("utf-8")
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
            return normalise_result(_parse_json(raw))
        except Exception as e:
            if "431" in str(e):
                log.debug(f"431 on {max_kb}KB image, retrying smaller")
                continue
            log.warning(f"Vision API failed for bytes input: {e}")
            return None
    log.warning("Vision API failed after all size retries")
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
