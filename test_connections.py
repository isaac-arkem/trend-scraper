"""Quick connection test — run before first pipeline run."""
import os
from dotenv import load_dotenv

load_dotenv()

print("=== Community Mapper — Connection Test ===\n")

# Test Apify
try:
    import httpx
    token = os.environ["APIFY_TOKEN"]
    resp = httpx.get(f"https://api.apify.com/v2/users/me?token={token}", timeout=8)
    data = resp.json().get("data", {})
    print(f"✓ Apify      user={data.get('username')}  plan={data.get('plan', {}).get('id')}")
except Exception as e:
    print(f"✗ Apify      {e}")

# Test MinIO
try:
    from minio import Minio
    endpoint = os.environ["MINIO_ENDPOINT"].replace("http://", "").replace("https://", "")
    secure = os.environ["MINIO_ENDPOINT"].startswith("https://")
    mc = Minio(
        endpoint,
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=secure,
        region="eu-south-1",
    )
    bucket = os.environ.get("MINIO_BUCKET", "social-intel")
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)
        print(f"✓ MinIO      bucket '{bucket}' created")
    else:
        print(f"✓ MinIO      bucket '{bucket}' ready")
except Exception as e:
    print(f"✗ MinIO      {e}")

print("\n✅ Ready to run pipeline")
