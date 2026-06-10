import os
import io
import httpx
from minio import Minio
from minio.error import S3Error
from dotenv import load_dotenv
from src.utils.logger import get_logger

load_dotenv()
log = get_logger(__name__)

_client: Minio | None = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        endpoint = os.environ["MINIO_ENDPOINT"].replace("http://", "").replace("https://", "")
        secure = os.environ["MINIO_ENDPOINT"].startswith("https://")
        _client = Minio(
            endpoint,
            access_key=os.environ["MINIO_ACCESS_KEY"],
            secret_key=os.environ["MINIO_SECRET_KEY"],
            secure=secure,
            region="eu-south-1",
        )
        bucket = os.environ.get("MINIO_BUCKET", "social-intel")
        if not _client.bucket_exists(bucket):
            _client.make_bucket(bucket)
            log.info(f"Created MinIO bucket: {bucket}")
    return _client


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
