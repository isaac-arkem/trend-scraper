import os
import io
import re
import threading
import httpx
from minio import Minio
from minio.error import S3Error
from dotenv import load_dotenv
from src.utils.logger import get_logger

load_dotenv()
log = get_logger(__name__)

# Thread-local: the MinIO client's urllib3 pool (default size 10) gets exhausted
# when shared across 16 workers. One client per thread keeps each pool to itself.
_local = threading.local()


# Which object store the pipeline writes to. MinIO lives on the office NAS and
# went unreachable twice in one day when the power tripped and again when the NAS
# moved office and changed address, so new runs default to Wasabi, which is
# off-site. Set OBJECT_STORE=minio to write locally again. Both are S3-compatible,
# so the same client and the same paths work against either — only the endpoint,
# credentials and region change.
OBJECT_STORE = (os.environ.get("OBJECT_STORE")
                or os.environ.get("TRIAGE_OBJECT_STORE")
                or "wasabi").lower()


def _backend() -> dict:
    if OBJECT_STORE == "minio":
        return {
            "endpoint": os.environ["MINIO_ENDPOINT"],
            "access_key": os.environ["MINIO_ACCESS_KEY"],
            "secret_key": os.environ["MINIO_SECRET_KEY"],
            "region": os.environ.get("MINIO_REGION", "eu-south-1"),
            "bucket": os.environ.get("MINIO_BUCKET", "social-intel"),
        }
    return {
        "endpoint": os.environ["WASABI_ENDPOINT_URL"],
        "access_key": os.environ["WASABI_ACCESS_KEY"],
        "secret_key": os.environ["WASABI_SECRET_KEY"],
        # Wasabi rejects a mismatched region on write, and the region is part of
        # the endpoint host, so derive it rather than hardcoding one.
        "region": os.environ.get("WASABI_REGION")
                  or (re.search(r"s3\.([a-z0-9-]+)\.wasabisys\.com",
                                os.environ["WASABI_ENDPOINT_URL"]) or [None, "us-east-1"])[1],
        "bucket": os.environ.get("WASABI_BUCKET", "social-intel"),
    }


# The pipeline thinks in two logical buckets — media and runs. MinIO had one
# bucket per logical name; the Wasabi key can only write to `hckd-crow`, so both
# live inside it, each under a prefix equal to its logical name.
#
# That keeps the move reversible: copying `hckd-crow/social-intel/**` into a real
# `social-intel` bucket leaves every key byte-identical, and namespacing switches
# itself off as soon as the physical bucket IS the logical one. No re-pathing, and
# nothing downstream has to know which arrangement is in force.
MEDIA_LOGICAL = os.environ.get("MEDIA_BUCKET_NAME", "social-intel")
RUNS_LOGICAL  = os.environ.get("TRENDS_BUCKET", "trends")


def bucket_name() -> str:
    return _backend()["bucket"]


def namespaced() -> bool:
    return bucket_name() not in (MEDIA_LOGICAL, RUNS_LOGICAL)


def resolve(logical: str, path: str) -> tuple:
    """(logical bucket, key) -> (bucket to write to, key to write at)."""
    physical = bucket_name()
    if namespaced():
        return physical, f"{logical}/{path.lstrip('/')}"
    return logical, path


def get_minio() -> Minio:
    """S3 client for the configured backend. Name kept for the existing callers."""
    client = getattr(_local, "client", None)
    if client is None:
        cfg = _backend()
        endpoint = cfg["endpoint"].replace("http://", "").replace("https://", "")
        client = Minio(
            endpoint,
            access_key=cfg["access_key"],
            secret_key=cfg["secret_key"],
            secure=cfg["endpoint"].startswith("https://"),
            region=cfg["region"],
        )
        bucket = cfg["bucket"]
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
                log.info(f"Created bucket {bucket} on {OBJECT_STORE}")
        except S3Error as e:
            # A bucket that exists but is owned elsewhere, or a key without
            # list-bucket rights, must not stop uploads that would still succeed.
            log.warning(f"Could not verify bucket {bucket} on {OBJECT_STORE}: {e}")
        _local.client = client
        log.info(f"Object store: {OBJECT_STORE} -> {endpoint}/{bucket} ({cfg['region']})")
    return client


def upload_from_url(url: str, minio_path: str) -> str:
    """Download media from URL and upload to object storage. Returns minio_path."""
    bucket, key = resolve(MEDIA_LOGICAL, minio_path)
    client = get_minio()

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        data = resp.content
        content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]

        client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        log.debug(f"Uploaded {bucket}/{key} ({len(data)//1024}KB)")
        return minio_path
    except (httpx.HTTPError, S3Error) as e:
        log.warning(f"Failed to upload {url}: {e}")
        return None


def upload_bytes(data: bytes, minio_path: str, content_type: str = "image/jpeg") -> str:
    bucket, key = resolve(MEDIA_LOGICAL, minio_path)
    client = get_minio()
    try:
        client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return minio_path
    except S3Error as e:
        log.warning(f"MinIO upload failed for {minio_path}: {e}")
        return None


def get_presigned_url(minio_path: str, expires_hours: int = 24) -> str:
    from datetime import timedelta
    bucket, key = resolve(MEDIA_LOGICAL, minio_path)
    client = get_minio()
    return client.presigned_get_object(bucket, key, expires=timedelta(hours=expires_hours))


def build_path(platform: str, country_iso: str, platform_user_id: str, post_id: str, filename: str) -> str:
    """Standard MinIO path: platform/country/user_id/posts/post_id/filename"""
    return f"{platform}/{country_iso}/{platform_user_id}/posts/{post_id}/{filename}"


def profile_pic_path(platform: str, key: str) -> str:
    """Deterministic MinIO path for a creator's profile picture, keyed by
    platform_user_id (falls back to username). The dashboard builds the exact same
    path from the creator row, so no DB column is needed to find the image."""
    return f"profiles/{platform}/{key}.jpg"
