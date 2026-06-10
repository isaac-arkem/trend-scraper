import os
import tempfile
import subprocess
from pathlib import Path
from src.utils.logger import get_logger

log = get_logger(__name__)


def extract_frames(video_path: str, max_frames: int = 6, interval_secs: int = 2) -> list[bytes]:
    """Extract frames from a local video file. Returns list of JPEG bytes."""
    frames = []
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_pattern = os.path.join(tmpdir, "frame_%03d.jpg")
            cmd = [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps=1/{interval_secs},scale=720:-1",
                "-vframes", str(max_frames),
                "-q:v", "3",
                out_pattern,
                "-loglevel", "error",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                log.warning(f"ffmpeg error: {result.stderr.decode()[:200]}")
                return []

            frame_files = sorted(Path(tmpdir).glob("frame_*.jpg"))[:max_frames]
            for f in frame_files:
                frames.append(f.read_bytes())

    except subprocess.TimeoutExpired:
        log.warning(f"ffmpeg timed out on {video_path}")
    except Exception as e:
        log.warning(f"Frame extraction failed: {e}")

    log.debug(f"Extracted {len(frames)} frames from {video_path}")
    return frames


def download_video_and_extract(video_url: str, max_frames: int = 6) -> list[bytes]:
    """Download video from URL and extract frames. Returns JPEG bytes."""
    import httpx

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        with httpx.stream("GET", video_url, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=8192):
                    f.write(chunk)

        frames = extract_frames(tmp_path, max_frames=max_frames)
        os.unlink(tmp_path)
        return frames

    except Exception as e:
        log.warning(f"Failed to download/extract video {video_url[:60]}: {e}")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return []
