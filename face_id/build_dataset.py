"""Synthesize a YOLO face-detection dataset from the LNSONI face crops.

LNSONI images are tight 92x112 grayscale face crops with no box labels, so we paste
them onto face-free backgrounds (person-free COCO128 images + procedural clutter) at
random sizes and positions and write each paste rectangle as a YOLO label.

    python -m face_id.build_dataset [--train 6000 --val 600]
"""
import argparse
import random
import shutil
import zipfile
from pathlib import Path

import cv2
import numpy as np

from .common import COCO128_URL, WIDER_DIR, WORK_DIR, download, face_files, imread

W, H = 640, 480  # webcam-like canvas


def load_backgrounds():
    """COCO128 images with no 'person' labels, so no unlabeled faces sneak in as negatives."""
    root = WORK_DIR / "coco128"
    if not root.exists():
        zpath = download(COCO128_URL, WORK_DIR / "coco128.zip")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(WORK_DIR)
        zpath.unlink()
    bgs = []
    for img in sorted((root / "images" / "train2017").glob("*.jpg")):
        label = root / "labels" / "train2017" / f"{img.stem}.txt"
        classes = {line.split()[0] for line in label.read_text().splitlines()} if label.exists() else set()
        if "0" not in classes:
            bgs.append(imread(img, cv2.IMREAD_GRAYSCALE))
    return bgs


def procedural_bg(rng):
    g0, g1 = rng.integers(0, 256, 2)
    ramp = np.linspace(g0, g1, W if rng.random() < 0.5 else H)
    bg = np.broadcast_to(ramp, (H, W)) if ramp.size == W else np.broadcast_to(ramp[:, None], (H, W))
    bg = bg.astype(np.float32).copy()
    for _ in range(rng.integers(3, 25)):
        color = float(rng.integers(0, 256))
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        s = int(rng.integers(5, 200))
        kind = rng.integers(3)
        if kind == 0:
            cv2.rectangle(bg, (x, y), (x + s, y + int(rng.integers(5, 200))), color, -1)
        elif kind == 1:
            cv2.circle(bg, (x, y), s // 2, color, -1)
        else:
            cv2.line(bg, (x, y), (int(rng.integers(0, W)), int(rng.integers(0, H))), color, int(rng.integers(1, 8)))
    bg = cv2.GaussianBlur(bg, (0, 0), float(rng.uniform(0.5, 6)))
    return bg + rng.normal(0, rng.uniform(0, 12), bg.shape).astype(np.float32)


def random_bg(rng, bgs):
    if rng.random() < 0.8:
        src = bgs[rng.integers(len(bgs))]
        sh, sw = src.shape
        scale = rng.uniform(0.4, 1.0)
        cw, ch = int(sw * scale), int(sh * scale)
        x, y = int(rng.integers(0, sw - cw + 1)), int(rng.integers(0, sh - ch + 1))
        bg = cv2.resize(src[y:y + ch, x:x + cw], (W, H)).astype(np.float32)
        if rng.random() < 0.5:
            bg = bg[:, ::-1]
    else:
        bg = procedural_bg(rng)
    return bg * rng.uniform(0.6, 1.3) + rng.uniform(-40, 40)


def feather_mask(fw, fh, rng):
    f = max(1.0, rng.uniform(0.03, 0.12) * min(fw, fh))
    rx = np.clip(np.minimum(np.arange(fw) + 1, fw - np.arange(fw)) / f, 0, 1)
    ry = np.clip(np.minimum(np.arange(fh) + 1, fh - np.arange(fh)) / f, 0, 1)
    return np.outer(ry, rx).astype(np.float32)


def augment_face(face, fw, fh, rng):
    interp = cv2.INTER_AREA if fh < face.shape[0] else cv2.INTER_LINEAR
    f = cv2.resize(face, (fw, fh), interpolation=interp).astype(np.float32)
    if rng.random() < 0.5:
        f = f[:, ::-1]
    f = f * rng.uniform(0.6, 1.4) + rng.uniform(-50, 50)
    if rng.random() < 0.3:
        gamma = rng.uniform(0.6, 1.6)
        f = 255 * (np.clip(f, 0, 255) / 255) ** gamma
    if rng.random() < 0.25:
        f = cv2.GaussianBlur(f, (0, 0), rng.uniform(0.5, 2.0))
    return f


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def make_image(rng, bgs, faces):
    canvas = random_bg(rng, bgs)
    r = rng.random()
    n = 0 if r < 0.1 else 1 if r < 0.55 else int(rng.integers(2, 6))
    max_h = 0.95 * H if n == 1 else 0.5 * H
    boxes = []
    for _ in range(n):
        for _attempt in range(20):
            fh = int(np.exp(rng.uniform(np.log(20), np.log(max_h))))
            fw = max(8, int(fh * 92 / 112 * rng.uniform(0.9, 1.1)))
            if fw >= W:
                continue
            x, y = int(rng.integers(0, W - fw + 1)), int(rng.integers(0, H - fh + 1))
            box = (x, y, x + fw, y + fh)
            if all(iou(box, b) < 0.05 for b in boxes):
                break
        else:
            continue
        face = augment_face(faces[rng.integers(len(faces))], fw, fh, rng)
        m = feather_mask(fw, fh, rng)
        region = canvas[y:y + fh, x:x + fw]
        canvas[y:y + fh, x:x + fw] = region * (1 - m) + face * m
        boxes.append(box)
    canvas += rng.normal(0, rng.uniform(0, 6), canvas.shape).astype(np.float32)
    return np.clip(canvas, 0, 255).astype(np.uint8), boxes


def write_split(name, count, rng, bgs, faces, out):
    img_dir, lbl_dir = out / "images" / name, out / "labels" / name
    img_dir.mkdir(parents=True)
    lbl_dir.mkdir(parents=True)
    for i in range(count):
        img, boxes = make_image(rng, bgs, faces)
        cv2.imwrite(str(img_dir / f"{i:05d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(60, 96))])
        (lbl_dir / f"{i:05d}.txt").write_text("".join(
            f"0 {(x1 + x2) / 2 / W:.6f} {(y1 + y2) / 2 / H:.6f} {(x2 - x1) / W:.6f} {(y2 - y1) / H:.6f}\n"
            for x1, y1, x2, y2 in boxes))
        if (i + 1) % 1000 == 0:
            print(f"  {name}: {i + 1}/{count}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", type=int, default=6000)
    ap.add_argument("--val", type=int, default=600)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)
    files = face_files()
    if not files:
        raise SystemExit("No faces found in dataset/LNSONI Human Face Dataset")
    # Hold out every 10th face so val measures detection of unseen face crops.
    train_faces = [imread(p, cv2.IMREAD_GRAYSCALE) for p in files if int(p.stem) % 10]
    val_faces = [imread(p, cv2.IMREAD_GRAYSCALE) for p in files if int(p.stem) % 10 == 0]
    bgs = load_backgrounds()
    print(f"{len(train_faces)} train faces, {len(val_faces)} val faces, {len(bgs)} COCO backgrounds")

    out = WORK_DIR / "yolo"
    if out.exists():
        shutil.rmtree(out)
    write_split("train", args.train, rng, bgs, train_faces, out)
    write_split("val", args.val, rng, bgs, val_faces, out)
    write_data_yaml(out)


def write_data_yaml(out):
    """Synthetic LNSONI composites, plus real WIDER FACE photos when prepare_wider has been run.

    Pasted crops alone teach the detector to find paste artifacts rather than faces, so real
    photos are what make it work on a webcam; WIDER val also makes mAP measure real images.
    """
    train, val = [out / "images" / "train"], [out / "images" / "val"]
    if (WIDER_DIR / "WIDER_train" / "labels").exists():
        train.append(WIDER_DIR / "WIDER_train" / "images")
        val.append(WIDER_DIR / "WIDER_val" / "images")
    else:
        print("WARNING: no WIDER FACE labels (run `python -m face_id.prepare_wider`); synthetic data only")
    fmt = lambda dirs: "".join(f"  - {d.as_posix()}\n" for d in dirs)
    (out / "data.yaml").write_text(f"train:\n{fmt(train)}val:\n{fmt(val)}names:\n  0: face\n")
    print(f"Wrote {out / 'data.yaml'}")


if __name__ == "__main__":
    main()
