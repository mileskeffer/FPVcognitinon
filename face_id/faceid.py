"""Two-model face ID: YOLO11n (trained on WIDER FACE + LNSONI) finds faces, SFace says whose they are.

Known people are photos in face_id/known_faces/, either <name>.jpg or <name>/*.jpg.
Each person's match threshold is calibrated so that at most 0.1% of the ~9.5k LNSONI
strangers would be accepted as them; anything below it is reported as "Unknown".

    python -m face_id.faceid enroll Miles              # webcam: SPACE saves a photo, Q quits
    python -m face_id.faceid enroll Miles --photo me.jpg
    python -m face_id.faceid identify photo.jpg        # print the name(s) in a photo
    python -m face_id.faceid identify --webcam         # one webcam snapshot -> name
    python -m face_id.faceid live                      # live webcam overlay
    python -m face_id.faceid scan [--watch]            # name everyone in face_id/screenshots/

`scan` writes face_id/screenshots/results.csv (one row per face) and annotated copies
to face_id/screenshots/annotated/. With --watch it keeps running and handles new
screenshots as they're dropped in.
"""
import argparse
import csv
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from .common import (DET_ONNX, DET_WEIGHTS, KNOWN_DIR, MODELS_DIR, SCREENSHOTS_DIR, SFACE_PATH, SFACE_URL,
                     download, face_files, gray3, imread, square_crop)
from .detector import FaceDetector

UNKNOWN = "Unknown"
BASE_THRESHOLD = 0.363  # OpenCV's recommended SFace cosine threshold
STRANGER_FAR = 0.001    # allowed false-accept rate against the LNSONI strangers
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def detector_weights():
    """ONNX (no torch needed) when exported, else the Ultralytics .pt."""
    for w in (DET_ONNX, DET_WEIGHTS):
        if w.exists():
            return w
    raise SystemExit(f"No detector in {MODELS_DIR} - run `python -m face_id.train` (or copy face_det.onnx there)")


class FaceID:
    def __init__(self, known_dir=KNOWN_DIR, conf=0.5):
        download(SFACE_URL, SFACE_PATH)
        self.det = FaceDetector(detector_weights(), conf=conf)
        self.rec = cv2.FaceRecognizerSF.create(str(SFACE_PATH), "")
        self.people = {}  # name -> (embeddings [n, 128], threshold)
        self.load_gallery(known_dir)

    def detect(self, img):
        """Face boxes [(x1, y1, x2, y2, conf)], largest first."""
        return sorted(self.det(img), key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)

    def embed(self, img, box=None):
        f = self.rec.feature(square_crop(gray3(img), box)).flatten()
        return f / np.linalg.norm(f)

    def stranger_embeddings(self):
        """SFace embeddings of the LNSONI faces, cached in models/ so other machines only need the .npy."""
        cache = MODELS_DIR / "lnsoni_sface.npy"
        files = face_files()
        if cache.exists():
            embs = np.load(cache)
            if not files or len(embs) == len(files):
                return embs
        if not files:
            print(f"  no {cache.name} or LNSONI dataset; using the default threshold {BASE_THRESHOLD}")
            return None
        print(f"Embedding {len(files)} LNSONI faces for threshold calibration (one-time)...")
        # LNSONI images are already face crops, so they're embedded whole rather than via the detector.
        embs = np.stack([self.embed(imread(p)) for p in files]).astype(np.float32)
        np.save(cache, embs)
        return embs

    def load_gallery(self, known_dir):
        known_dir = Path(known_dir)
        photos = {}
        for p in sorted(known_dir.glob("*")) if known_dir.exists() else []:
            if p.is_dir():
                photos.setdefault(p.name, []).extend(f for f in sorted(p.iterdir()) if f.suffix.lower() in IMAGE_EXTS)
            elif p.suffix.lower() in IMAGE_EXTS:
                photos.setdefault(p.stem, []).append(p)
        if not photos:
            print(f"No known faces in {known_dir} - everyone will be '{UNKNOWN}'. "
                  "Add some with `python -m face_id.faceid enroll NAME`.")
            return

        strangers = self.stranger_embeddings()
        for name, files in photos.items():
            embs = []
            for f in files:
                img = imread(f)
                if img is None:
                    print(f"  skipping unreadable {f}")
                    continue
                boxes = self.detect(img)
                if not boxes:
                    print(f"  no face detected in {f}; using the whole image as the face")
                embs.append(self.embed(img, boxes[0][:4] if boxes else None))
            if not embs:
                continue
            embs = np.stack(embs)
            threshold = BASE_THRESHOLD
            if strangers is not None:
                stranger_scores = (strangers @ embs.T).max(axis=1)
                threshold = max(BASE_THRESHOLD, float(np.quantile(stranger_scores, 1 - STRANGER_FAR)))
            self.people[name] = (embs, threshold)
            print(f"  {name}: {len(embs)} photo(s), match threshold {threshold:.3f}")

    def recognize(self, img, box):
        """(name, score) for the face in box; name is UNKNOWN below that person's threshold."""
        e = self.embed(img, box)
        name, score = UNKNOWN, -1.0
        for person, (embs, threshold) in self.people.items():
            s = float((embs @ e).max())
            if s > score:
                score = s
                name = person if s >= threshold else UNKNOWN
        return name, score

    def identify(self, img):
        """[{name, score, box}] for every face in a BGR image, largest face first."""
        results = []
        for *box, _conf in self.detect(img):
            name, score = self.recognize(img, box)
            results.append({"name": name, "score": score, "box": tuple(box)})
        return results


def open_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(f"Could not open camera {index}")
    for _ in range(10):  # let auto-exposure settle
        cap.read()
    return cap


def draw(frame, results):
    for r in results:
        x1, y1, x2, y2 = r["box"]
        color = (0, 0, 255) if r["name"] == UNKNOWN else (0, 200, 0)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, f"{r['name']} {r['score']:.2f}", (x1, max(15, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return frame


def cmd_enroll(args):
    dest = KNOWN_DIR / args.name
    dest.mkdir(parents=True, exist_ok=True)
    if args.photo:
        out = dest / f"{int(time.time())}{Path(args.photo).suffix.lower()}"
        shutil.copy2(args.photo, out)
        print(f"Saved {out}")
        return

    det = FaceDetector(detector_weights(), conf=args.conf)
    cap = open_camera(args.camera)
    saved = 0
    print("SPACE = save photo, Q/ESC = done. Take a few with slightly different angles/lighting.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        boxes = det(frame)
        view = frame.copy()
        for x1, y1, x2, y2, _conf in boxes:
            cv2.rectangle(view, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(view, f"{args.name}: {saved} saved", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 0), 2)
        cv2.imshow("enroll", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            if len(boxes) != 1:
                print(f"  need exactly one face in frame (found {len(boxes)})")
                continue
            out = dest / f"{int(time.time() * 1000)}.jpg"
            cv2.imwrite(str(out), frame)
            saved += 1
            print(f"  saved {out}")
        elif key in (ord("q"), 27):
            break
    cap.release()
    cv2.destroyAllWindows()


def cmd_identify(args):
    if args.webcam:
        cap = open_camera(args.camera)
        ok, img = cap.read()
        cap.release()
        if not ok:
            raise SystemExit("Could not read a frame from the webcam")
    elif args.image:
        img = imread(args.image)
        if img is None:
            raise SystemExit(f"Could not read {args.image}")
    else:
        raise SystemExit("Give an image path or --webcam")

    fid = FaceID(conf=args.conf)
    results = fid.identify(img)
    if not results:
        print("No face found")
    for r in results:
        print(f"{r['name']}\t(score {r['score']:.3f}, box {r['box']})")
    if args.save:
        cv2.imwrite(args.save, draw(img, results))


def cmd_live(args):
    fid = FaceID(conf=args.conf)
    cap = open_camera(args.camera)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cv2.imshow("face id", draw(frame, fid.identify(frame)))
        if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
            break
    cap.release()
    cv2.destroyAllWindows()


def cmd_scan(args):
    folder = Path(args.folder)
    annotated = folder / "annotated"
    annotated.mkdir(parents=True, exist_ok=True)
    fid = FaceID(conf=args.conf)
    csv_path = folder / "results.csv"
    fields = ["file", "name", "score", "x1", "y1", "x2", "y2"]
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerow(fields)

    def pending():
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    def process(path):
        img = imread(path)
        if img is None:
            return False  # likely still being written; retry next pass
        results = fid.identify(img)
        names = ", ".join(r["name"] for r in results) or "no face found"
        print(f"{path.name}: {names}")
        cv2.imwrite(str(annotated / f"{path.stem}.jpg"), draw(img, results))
        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            for r in results:
                w.writerow([path.name, r["name"], f"{r['score']:.3f}", *r["box"]])
            if not results:
                w.writerow([path.name, "", "", "", "", "", ""])
        return True

    done = set()
    for p in pending():
        if process(p):
            done.add(p)
    if not args.watch:
        print(f"{len(done)} screenshot(s) -> {csv_path}")
        return

    print(f"Watching {folder} for new screenshots (Ctrl+C to stop)...")
    sizes = {}
    try:
        while True:
            for p in pending():
                if p in done:
                    continue
                # Only read a file once its size stops changing, so half-written saves are skipped.
                size = p.stat().st_size
                if sizes.get(p) == size and process(p):
                    done.add(p)
                sizes[p] = size
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--conf", type=float, default=0.5, help="face detector confidence")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enroll", help="add photos of a known person")
    p.add_argument("name")
    p.add_argument("--photo", help="copy this photo instead of using the webcam")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("identify", help="name the face(s) in one photo or webcam snapshot")
    p.add_argument("image", nargs="?")
    p.add_argument("--webcam", action="store_true")
    p.add_argument("--save", help="write an annotated copy here")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("live", help="live webcam recognition")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("scan", help="name the faces in every screenshot in a folder")
    p.add_argument("--folder", default=str(SCREENSHOTS_DIR))
    p.add_argument("--watch", action="store_true", help="keep running and process new screenshots")
    p.set_defaults(func=cmd_scan)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
