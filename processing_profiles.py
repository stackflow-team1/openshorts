import os


PROFILES = frozenset({"QUALITY", "BALANCED", "FAST"})


def normalize_profile(value):
    profile = str(value or "QUALITY").strip().upper()
    if profile not in PROFILES:
        raise ValueError(f"Unknown processing profile: {value!r}")
    return profile


def current_profile():
    return normalize_profile(os.environ.get("PROCESSING_PROFILE", "QUALITY"))


def fast_preview_enabled():
    return current_profile() == "FAST" and os.environ.get("FAST_PREVIEW", "1") != "0"
