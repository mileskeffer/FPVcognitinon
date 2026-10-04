"""Export the trained face detector to ONNX for the streaming laptop.

    python -m face_id.export [--imgsz 640]

Writes face_id/models/face_det.onnx. 640 keeps small/distant faces; 416 is ~2x faster on CPU.
"""
import argparse
import shutil

from ultralytics import YOLO

from .common import DET_ONNX, DET_WEIGHTS


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()

    out = YOLO(str(DET_WEIGHTS)).export(format="onnx", imgsz=args.imgsz, simplify=True, dynamic=False)
    shutil.move(out, DET_ONNX)
    print(f"Wrote {DET_ONNX} ({args.imgsz}x{args.imgsz})")


if __name__ == "__main__":
    main()
