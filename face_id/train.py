"""Fine-tune YOLO11n as a single-class face detector on the synthetic LNSONI dataset.

    python -m face_id.build_dataset   # first
    python -m face_id.train [--epochs 50 --batch 16]

The best weights are copied to face_id/models/face_det.pt.
"""
import argparse
import os
import shutil

from ultralytics import YOLO

from .common import DET_WEIGHTS, WORK_DIR


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="0")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--name", default="face_det", help="run name under WORK_DIR/runs")
    args = ap.parse_args()

    data = WORK_DIR / "yolo" / "data.yaml"
    if not data.exists():
        raise SystemExit(f"{data} not found - run `python -m face_id.build_dataset` first")
    # Ultralytics drops yolo11n.pt (and the AMP-check copy) in the cwd; keep that out of the repo.
    os.chdir(WORK_DIR)

    model = YOLO("yolo11n.pt")
    model.train(
        data=str(data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=args.workers,
        project=str(WORK_DIR / "runs"),
        name=args.name,
        exist_ok=True,
        degrees=5.0,
        patience=15,
    )
    best = WORK_DIR / "runs" / args.name / "weights" / "best.pt"
    DET_WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, DET_WEIGHTS)
    print(f"Copied {best} -> {DET_WEIGHTS}")


if __name__ == "__main__":
    main()
