"""Content-addressed, expiring cache for completed local transcriptions."""
import hashlib
import json
import os
import tempfile
import time


_VERSION = 1
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
_HASH_CHUNK_BYTES = 4 * 1024 * 1024


def _cache_dir():
    return os.environ.get(
        "TRANSCRIPT_CACHE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "openshorts", "transcripts"))


def _ttl_seconds():
    try:
        return max(0, int(os.environ.get(
            "TRANSCRIPT_CACHE_HOURS", str(_DEFAULT_TTL_SECONDS // 3600))) * 3600)
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def cache_key(video_path):
    """Hash the source incrementally and include the exact ASR configuration."""
    from subtitles import WHISPER_TRANSCRIBE_PARAMS, get_whisper_config

    digest = hashlib.sha256()
    with open(video_path, "rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    settings = {
        "version": _VERSION,
        "source_sha256": digest.hexdigest(),
        "backend": os.environ.get("TRANSCRIBE_BACKEND", "whisper").strip().lower(),
        "whisper": get_whisper_config(),
        "params": WHISPER_TRANSCRIBE_PARAMS,
    }
    return hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _path(key):
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid transcript cache key")
    return os.path.join(_cache_dir(), f"{key}.json")


def load(key):
    path = _path(key)
    ttl = _ttl_seconds()
    if ttl <= 0:
        return None
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            os.remove(path)
            return None
        with open(path, encoding="utf-8") as source:
            entry = json.load(source)
        transcript = entry.get("transcript")
        if (entry.get("version") != _VERSION
                or not isinstance(transcript, dict)
                or not isinstance(transcript.get("segments"), list)
                or not transcript["segments"]):
            raise ValueError("cached transcript has an invalid shape")
        return transcript
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        print(f"⚠️ Ignoring unusable transcript cache entry: {error}", flush=True)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None


def store(key, transcript):
    """Persist only a completed transcript; a cache failure never fails a job."""
    if _ttl_seconds() <= 0:
        return
    if (not isinstance(transcript, dict)
            or not isinstance(transcript.get("segments"), list)
            or not transcript["segments"]):
        return
    path = _path(key)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".transcript-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump({"version": _VERSION, "transcript": transcript}, target)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        _expire_entries(directory, path)
    except OSError as error:
        print(f"⚠️ Could not store transcript cache entry: {error}", flush=True)


def _expire_entries(directory, just_written):
    ttl = _ttl_seconds()
    now = time.time()
    entries = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue
        if now - stat.st_mtime > ttl:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        else:
            entries.append((stat.st_mtime, path))
    max_entries = 500
    for _, path in sorted(entries)[:-max_entries]:
        if path != just_written:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
