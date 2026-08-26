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
    # Test whatever store is configured, not MinIO specifically — after the
    # Wasabi move a green MinIO check said nothing about where data goes.
    from src.storage.minio import get_minio, bucket_name, OBJECT_STORE
    client = get_minio()
    bucket = bucket_name()
    print(f"  store: {OBJECT_STORE} -> {bucket}")
    if not mc.bucket_exists(bucket):
        mc.make_bucket(bucket)
        print(f"✓ MinIO      bucket '{bucket}' created")
    else:
        print(f"✓ MinIO      bucket '{bucket}' ready")
except Exception as e:
    print(f"✗ MinIO      {e}")

print("\n✅ Ready to run pipeline")
