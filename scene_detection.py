"""Scene detection engines for the reframe pipeline.

Primary engine: TransNetV2, a small neural shot-boundary detector that is
markedly more accurate than threshold-based detection (handles fast camera
motion, flashes and gradual transitions such as fades/dissolves). Runs on
48x27 frames, faster than realtime even on CPU.

Fallback/legacy engine: PySceneDetect ContentDetector, byte-for-byte the
pre-existing behavior. Any TransNetV2 failure (missing package, corrupt
weights, decode error) falls back automatically so a scene-detection edge
case can never kill a job.

Environment variables:
  SCENE_ENGINE          "transnetv2" (default) | "pyscenedetect" (legacy)
  SCENE_MIN_SEC         minimum scene length in seconds; shorter scenes are
                        merged into a neighbor (default 0.4, TransNetV2 path
                        only — the legacy path stays untouched)
  TRANSNETV2_THRESHOLD  shot-boundary probability threshold (default 0.5)
  TRANSNETV2_DEVICE     torch device: "auto" (default) | "cpu" | "cuda" | "mps"
"""

import os
import subprocess
import threading

import cv2
import numpy as np
from scenedetect import open_video, SceneManager, FrameTimecode
from scenedetect.detectors import ContentDetector

import gpu_decode
from stage_timing import timed_stage

# TransNetV2 input size (width x height), fixed by the trained model.
_TN2_W, _TN2_H = 48, 27

# One shared model instance; clips can process in parallel (CLIP_WORKERS) so
# inference is serialized like the other detectors in main.py.
_TN2_LOCK = threading.Lock()
_tn2_model = None
_SCENE_CACHE_LOCK = threading.Lock()
_SCENE_CACHE = {}


def _scene_cache_key(video_path):
    stat = os.stat(video_path)
    return (
        os.path.abspath(video_path), stat.st_size, stat.st_mtime_ns,
        os.environ.get("SCENE_ENGINE", "transnetv2").strip().lower(),
        os.environ.get("SCENE_MIN_SEC", "0.4"),
        os.environ.get("TRANSNETV2_THRESHOLD", "0.5"),
        os.environ.get("TRANSNETV2_DEVICE", "auto"),
        os.environ.get("SCENE_DETECTION", "1"),
    )


def detect_scenes(video_path):
    """Detect scenes. Returns (scene_list, fps) where scene_list is a list of
    (FrameTimecode, FrameTimecode) pairs — the same contract PySceneDetect's
    SceneManager.get_scene_list() has always given callers."""
    key = _scene_cache_key(video_path)
    with _SCENE_CACHE_LOCK:
        cached = _SCENE_CACHE.get(key)
    if cached is not None:
        print("   ♻️ Reusing cached scene analysis.")
        return list(cached[0]), cached[1]

    with timed_stage("scene_detection"):
        if os.environ.get("SCENE_DETECTION", "1").strip().lower() in ("0", "false", "off"):
            cap = cv2.VideoCapture(video_path)
            try:
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            finally:
                cap.release()
            if frame_count <= 0:
                raise RuntimeError("Cannot disable scene detection: video has no readable frames")
            result = ([(FrameTimecode(0, fps), FrameTimecode(frame_count, fps))], fps)
        else:
            engine = os.environ.get("SCENE_ENGINE", "transnetv2").strip().lower()
            if engine != "pyscenedetect":
                try:
                    result = _detect_transnetv2(video_path)
                except Exception as e:
                    print(f"   ⚠️ TransNetV2 scene detection failed "
                          f"({type(e).__name__}: {e}) — falling back to PySceneDetect")
                    result = _detect_pyscenedetect(video_path)
            else:
                result = _detect_pyscenedetect(video_path)
    with _SCENE_CACHE_LOCK:
        _SCENE_CACHE[key] = result
        if len(_SCENE_CACHE) > 64:
            _SCENE_CACHE.pop(next(iter(_SCENE_CACHE)))
    return list(result[0]), result[1]


# --- legacy engine ----------------------------------------------------------

def _detect_pyscenedetect(video_path):
    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector())
    scene_manager.detect_scenes(video=video)
    scene_list = scene_manager.get_scene_list()
    fps = video.frame_rate
    return scene_list, fps


# --- TransNetV2 engine ------------------------------------------------------

def _get_tn2_model():
    global _tn2_model
    if _tn2_model is None:
        from transnetv2_pytorch import TransNetV2
        device = os.environ.get("TRANSNETV2_DEVICE", "auto")
        model = TransNetV2(device=device)
        model.eval()
        _tn2_model = model
    return _tn2_model


def _extract_frames_small(video_path):
    """Decode the whole clip as 48x27 RGB frames via ffmpeg (~4KB/frame).

    Decoded on the card when it can (gpu_decode: same frames). The number of
    frames decoded is remembered for the trajectory pass
    (``gpu_decode.decoded_frames``)."""
    def build(on_gpu):
        return ["ffmpeg", "-nostdin", *gpu_decode.input_args(on_gpu), "-i", video_path,
                "-vf", gpu_decode.vf(f"scale={_TN2_W}:{_TN2_H}", on_gpu),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]

    def runner(cmd):
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=900)

    proc = gpu_decode.run(build, runner, ok=lambda r: r.returncode == 0 and r.stdout,
                          path=video_path, render=False)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args)
    frame_bytes = _TN2_H * _TN2_W * 3
    n = len(proc.stdout) // frame_bytes
    if n == 0:
        raise RuntimeError("ffmpeg produced no frames")
    if gpu_decode.one_frame_per_decoded_frame(proc.stderr):
        gpu_decode.remember_count(video_path, n)
    return np.frombuffer(proc.stdout[:n * frame_bytes],
                         dtype=np.uint8).reshape(n, _TN2_H, _TN2_W, 3)


def _detect_transnetv2(video_path):
    import torch

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    frames = _extract_frames_small(video_path)
    model = _get_tn2_model()
    threshold = float(os.environ.get("TRANSNETV2_THRESHOLD", "0.5"))

    with _TN2_LOCK, torch.no_grad():
        tensor = torch.from_numpy(np.ascontiguousarray(frames)).to(model.device)
        single_frame_pred, _ = model.predict_frames(tensor, quiet=True)
        single_frame_pred = single_frame_pred.cpu()
        del tensor
        # torch's caching allocator keeps the peak of this pass reserved for
        # the rest of the process; eight jobs doing that filled a 20 GB card.
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # predictions_to_scenes returns [[start, end], ...] with INCLUSIVE ends;
    # downstream expects PySceneDetect's exclusive ends.
    raw = model.predictions_to_scenes(single_frame_pred.numpy(), threshold=threshold)
    bounds = [(int(s), int(e) + 1) for s, e in raw]

    # cv2's frame count can differ by a few frames from what ffmpeg decodes;
    # downstream loops run to the decoder's count, so cover the gap.
    if total_frames > bounds[-1][1]:
        bounds[-1] = (bounds[-1][0], total_frames)

    min_sec = float(os.environ.get("SCENE_MIN_SEC", "0.4"))
    bounds = _merge_short_scenes(bounds, fps, min_sec)

    scene_list = [(FrameTimecode(s, fps), FrameTimecode(e, fps))
                  for s, e in bounds]
    print(f"   🎬 Scene engine: TransNetV2 — {len(scene_list)} scenes")
    return scene_list, fps


def _merge_short_scenes(bounds, fps, min_sec):
    """Absorb scenes shorter than min_sec into a neighbor. Ultra-short scenes
    cause camera-snap bursts and starve the per-scene strategy sampling."""
    if len(bounds) <= 1 or min_sec <= 0:
        return bounds
    min_frames = max(1, int(round(min_sec * float(fps))))

    merged = []
    for s, e in bounds:
        if merged and (e - s) < min_frames:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    # The first scene can still be short — fold it into the one after it.
    if len(merged) > 1 and (merged[0][1] - merged[0][0]) < min_frames:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged
