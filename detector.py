"""Damage detection with the custom YOLOv8 model, plus an optional "is this a vehicle?" check.

Models load lazily on first use so the web app starts instantly and keeps running even if a
model file or library is missing (the UI shows the detector status instead of crashing).
"""
import hashlib
import os
import threading

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DAMAGE_WEIGHTS = os.getenv("DAMAGE_WEIGHTS", os.path.join(BASE_DIR, "models", "best.pt"))
# Standard COCO model used only to confirm a vehicle is in the photo. Ultralytics downloads it
# automatically on first use (~6 MB). Set VEHICLE_WEIGHTS="" to disable.
VEHICLE_WEIGHTS = os.getenv("VEHICLE_WEIGHTS", "yolov8n.pt")
VEHICLE_CLASSES = {"car", "motorcycle", "bus", "truck"}

# DETECTOR_MODE=demo produces deterministic fake detections so the app can be tried without
# PyTorch installed. Never use it for real assessments.
DETECTOR_MODE = os.getenv("DETECTOR_MODE", "auto").lower()

# Grease-pencil amber for marks, BGR
MARK = (5, 183, 242)
INK = (40, 35, 31)

_lock = threading.Lock()
_infer_lock = threading.Lock()  # YOLO predictors aren't thread-safe; the dev server is threaded
_damage_model = None
_vehicle_model = None
_load_error = None
_vehicle_error = None


class DetectorUnavailable(RuntimeError):
    pass


def _load_damage_model():
    global _damage_model, _load_error
    if _damage_model is not None or DETECTOR_MODE == "demo":
        return _damage_model
    with _lock:
        if _damage_model is None:
            try:
                from ultralytics import YOLO
                _damage_model = YOLO(DAMAGE_WEIGHTS)
                _load_error = None
            except Exception as e:  # noqa: BLE001
                _load_error = f"{type(e).__name__}: {e}"
                raise DetectorUnavailable(_load_error) from e
    return _damage_model


def _load_vehicle_model():
    global _vehicle_model, _vehicle_error
    if not VEHICLE_WEIGHTS or DETECTOR_MODE == "demo":
        return None
    if _vehicle_model is not None or _vehicle_error:
        return _vehicle_model
    with _lock:
        if _vehicle_model is None and not _vehicle_error:
            try:
                from ultralytics import YOLO
                _vehicle_model = YOLO(VEHICLE_WEIGHTS)
            except Exception as e:  # noqa: BLE001
                _vehicle_error = str(e)
                print(f"[detector] vehicle check disabled: {e}")
    return _vehicle_model


def status():
    if DETECTOR_MODE == "demo":
        return {"mode": "demo", "ready": True, "message": "Demo mode: detections are simulated."}
    try:
        import ultralytics  # noqa: F401
    except Exception:
        return {"mode": "missing", "ready": False,
                "message": "The 'ultralytics' package is not installed. Run: pip install -r requirements.txt"}
    if not os.path.exists(DAMAGE_WEIGHTS):
        return {"mode": "missing", "ready": False, "message": f"Model weights not found at {DAMAGE_WEIGHTS}"}
    if _load_error:
        return {"mode": "error", "ready": False, "message": _load_error}
    return {"mode": "yolo", "ready": True,
            "message": "YOLOv8 model loaded" if _damage_model else "YOLOv8 model loads on first scan"}


def contains_vehicle(img):
    """True/False, or None when the check could not run (treated as 'unknown, allow')."""
    model = _load_vehicle_model()
    if model is None:
        return None
    try:
        with _infer_lock:
            res = model(img, conf=0.3, verbose=False)[0]
        names = res.names
        return any(names[int(c)] in VEHICLE_CLASSES for c in res.boxes.cls.tolist())
    except Exception as e:  # noqa: BLE001
        print(f"[detector] vehicle check failed: {e}")
        return None


def detect(img, conf=0.25):
    """Run damage detection on a BGR image. Returns a list of
    {part, confidence, box:[x1,y1,x2,y2] in 0..1, area}."""
    h, w = img.shape[:2]
    if DETECTOR_MODE == "demo":
        return _demo_detect(img, conf)
    model = _load_damage_model()
    with _infer_lock:
        res = model(img, conf=conf, verbose=False)[0]
    names = res.names
    out = []
    for xyxy, c, cls in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
        x1, y1, x2, y2 = xyxy
        box = [max(0, x1 / w), max(0, y1 / h), min(1, x2 / w), min(1, y2 / h)]
        out.append({
            "part": str(names[int(cls)]).strip().title(),
            "confidence": round(float(c), 3),
            "box": [round(v, 4) for v in box],
            "area": round((box[2] - box[0]) * (box[3] - box[1]), 4),
        })
    out.sort(key=lambda d: -d["confidence"])
    return out


def _demo_detect(img, conf):
    from database import PARTS
    small = cv2.resize(img, (32, 32))
    seed = int(hashlib.md5(small.tobytes()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(int(rng.integers(1, 4))):
        x1, y1 = rng.uniform(0.05, 0.6), rng.uniform(0.1, 0.6)
        bw, bh = rng.uniform(0.15, 0.35), rng.uniform(0.12, 0.3)
        box = [x1, y1, min(0.98, x1 + bw), min(0.98, y1 + bh)]
        c = float(rng.uniform(0.35, 0.95))
        if c < conf:
            continue
        out.append({
            "part": PARTS[int(rng.integers(0, len(PARTS)))],
            "confidence": round(c, 3),
            "box": [round(v, 4) for v in box],
            "area": round((box[2] - box[0]) * (box[3] - box[1]), 4),
        })
    return sorted(out, key=lambda d: -d["confidence"])


def annotate(img, detections):
    """Draw corner-bracket marks with numbered tags, like an assessor's grease pencil."""
    out = img.copy()
    h, w = out.shape[:2]
    t = max(2, int(round(min(h, w) / 260)))
    font_scale = max(0.35, min(h, w) / 800)
    for i, d in enumerate(detections, 1):
        x1, y1, x2, y2 = [int(v * s) for v, s in zip(d["box"], (w, h, w, h))]
        L = max(12, int(min(x2 - x1, y2 - y1) * 0.22))
        cv2.rectangle(out, (x1, y1), (x2, y2), MARK, max(1, t // 2), cv2.LINE_AA)
        for (cx, cy, dx, dy) in ((x1, y1, 1, 1), (x2, y1, -1, 1), (x1, y2, 1, -1), (x2, y2, -1, -1)):
            cv2.line(out, (cx, cy), (cx + dx * L, cy), MARK, t * 2, cv2.LINE_AA)
            cv2.line(out, (cx, cy), (cx, cy + dy * L), MARK, t * 2, cv2.LINE_AA)
        label = f"{i}  {d['part']}  {int(d['confidence'] * 100)}%"
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, max(1, t // 2 + 1))
        ty = y1 - 8 if y1 - th - 14 > 0 else min(h - base - 2, y2 + th + 14)
        lx = max(0, min(x1, w - tw - 14))
        cv2.rectangle(out, (lx, ty - th - 8), (lx + tw + 14, ty + base), MARK, -1)
        cv2.putText(out, label, (lx + 7, ty - 3), cv2.FONT_HERSHEY_SIMPLEX, font_scale, INK,
                    max(1, t // 2 + 1), cv2.LINE_AA)
    return out


def read_image(data: bytes, max_side=1920):
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        try:
            from io import BytesIO
            from PIL import Image, ImageOps
            pil = ImageOps.exif_transpose(Image.open(BytesIO(data))).convert("RGB")
            img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        except Exception:
            return None
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img
