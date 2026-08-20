import os
import io
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


def get_minio() -> Minio:
    client = getattr(_local, "client", None)
    if client is None:
        endpoint = os.environ["MINIO_ENDPOINT"].replace("http://", "").replace("https://", "")
        secure = os.environ["MINIO_ENDPOINT"].startswith("https://")
        client = Minio(
            endpoint,
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=secure,
            region="eu-south-1",
        )
        bucket = os.environ.get("MINIO_BUCKET", "social-intel")
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            log.info(f"Created MinIO bucket: {bucket}")
        _local.client = client
    return client


def upload_from_url(url: str, minio_path: str) -> str:
    """Download media from URL and upload to MinIO. Returns the minio_path."""
    bucket = os.environ.get("MINIO_BUCKET", "social-intel")
    client = get_minio()

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        data = resp.content
        content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]

        client.put_object(
            bucket,
            minio_path,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        log.debug(f"Uploaded {minio_path} ({len(data)//1024}KB)")
        return minio_path
    except (httpx.HTTPError, S3Error) as e:
        log.warning(f"Failed to upload {url}: {e}")
        return None


def upload_bytes(data: bytes, minio_path: str, content_type: str = "image/jpeg") -> str:
    bucket = os.environ.get("MINIO_BUCKET", "social-intel")
    client = get_minio()
    try:
        client.put_object(
            bucket,
            minio_path,
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
    bucket = os.environ.get("MINIO_BUCKET", "social-intel")
    client = get_minio()
    return client.presigned_get_object(bucket, minio_path, expires=timedelta(hours=expires_hours))


def build_path(platform: str, country_iso: str, platform_user_id: str, post_id: str, filename: str) -> str:
    """Standard MinIO path: platform/country/user_id/posts/post_id/filename"""
    return f"{platform}/{country_iso}/{platform_user_id}/posts/{post_id}/{filename}"


def profile_pic_path(platform: str, key: str) -> str:
    """Deterministic MinIO path for a creator's profile picture, keyed by
    platform_user_id (falls back to username). The dashboard builds the exact same
    path from the creator row, so no DB column is needed to find the image."""
    return f"profiles/{platform}/{key}.jpg"
