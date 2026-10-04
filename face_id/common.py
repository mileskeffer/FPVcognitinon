"""Shared paths and preprocessing for the face ID pipeline."""
import os
import urllib.request
from pathlib import Path

import cv2
import numpy as np

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
FACES_DIR = REPO_DIR / "dataset" / "LNSONI Human Face Dataset"
WIDER_DIR = REPO_DIR / "dataset" / "WIDER_FACE"
MODELS_DIR = PKG_DIR / "models"
KNOWN_DIR = PKG_DIR / "known_faces"
SCREENSHOTS_DIR = PKG_DIR / "screenshots"
# Generated training data and YOLO runs live outside OneDrive so it doesn't sync thousands of files.
WORK_DIR = Path(os.environ.get("FACEID_WORK", "C:/datasets/fpv-faces"))

DET_WEIGHTS = MODELS_DIR / "face_det.pt"
DET_ONNX = MODELS_DIR / "face_det.onnx"  # from `python -m face_id.export`; preferred when present
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
SFACE_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_recognition_sface/face_recognition_sface_2021dec.onnx")
COCO128_URL = "https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip"

SFACE_SIZE = 112


def download(url, dest):
    dest = Path(dest)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {url} -> {dest}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return dest


def gray3(img):
    """LNSONI is grayscale, so the recognizer always sees grayscale (as 3-channel BGR) to match it."""
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def square_crop(img, box=None, size=SFACE_SIZE):
    """Square crop centred on box (x1, y1, x2, y2), side = longest edge, edges replicated.

    A full LNSONI image and a YOLO box around a webcam face both map to the same framing,
    which keeps the stranger set used for threshold calibration comparable to live faces.
    """
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box if box is not None else (0, 0, w, h)
    side = int(round(max(x2 - x1, y2 - y1)))
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    x0, y0 = int(round(cx - side / 2)), int(round(cy - side / 2))
    padded = cv2.copyMakeBorder(img, side, side, side, side, cv2.BORDER_REPLICATE)
    crop = padded[y0 + side:y0 + 2 * side, x0 + side:x0 + 2 * side]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA if side > size else cv2.INTER_LINEAR)


def face_files():
    return sorted(FACES_DIR.glob("*.jpg"), key=lambda p: int(p.stem))


def imread(path, flags=cv2.IMREAD_COLOR):
    # cv2.imread can't open non-ASCII Windows paths; decoding from bytes can.
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flags)
