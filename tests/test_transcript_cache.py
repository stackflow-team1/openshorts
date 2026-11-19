import transcript_cache


TRANSCRIPT = {
    "text": "hello there",
    "language": "en",
    "segments": [{"start": 0.0, "end": 1.0, "text": "hello there", "words": []}],
}


def test_cache_key_tracks_video_bytes_and_asr_configuration(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"first source")
    monkeypatch.setenv("TRANSCRIPT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PROCESSING_PROFILE", "QUALITY")
    key = transcript_cache.cache_key(source)
    assert transcript_cache.cache_key(source) == key

    source.write_bytes(b"changed source")
    assert transcript_cache.cache_key(source) != key
    source.write_bytes(b"first source")
    monkeypatch.setenv("WHISPER_MODEL_FAST", "tiny")
    monkeypatch.setenv("PROCESSING_PROFILE", "FAST")
    assert transcript_cache.cache_key(source) != key


def test_cache_stores_only_completed_transcripts_and_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_CACHE_DIR", str(tmp_path / "cache"))
    key = "a" * 64
    transcript_cache.store(key, TRANSCRIPT)
    assert transcript_cache.load(key) == TRANSCRIPT

    transcript_cache.store("b" * 64, {"segments": []})
    assert transcript_cache.load("b" * 64) is None

    monkeypatch.setenv("TRANSCRIPT_CACHE_HOURS", "0")
    assert transcript_cache.load(key) is None
