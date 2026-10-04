"""YOLO face detector with two backends.

- face_det.onnx -> ONNX Runtime directly. No torch/Ultralytics needed, which is what the
  streaming laptop wants (Ultralytics added ~40 ms/frame there for drone_detect.py).
- face_det.pt   -> Ultralytics, for the training machine.
"""
import os
from pathlib import Path

import cv2
import numpy as np


class FaceDetector:
    def __init__(self, weights, conf=0.5, iou=0.5):
        self.weights = Path(weights)
        self.conf, self.iou = conf, iou
        if self.weights.suffix == ".onnx":
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(os.environ.get("ORT_THREADS", os.cpu_count() or 4))
            wanted = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            providers = [p for p in wanted if p in ort.get_available_providers()]
            self.session = ort.InferenceSession(str(self.weights), opts, providers=providers)
            inp = self.session.get_inputs()[0]
            self.input_name = inp.name
            self.size = int(inp.shape[2])  # exported with a fixed square imgsz
            self.yolo = None
        else:
            from ultralytics import YOLO

            self.yolo = YOLO(str(self.weights))
            self.size = None

    def __call__(self, img):
        """Face boxes [(x1, y1, x2, y2, conf)] for a BGR image."""
        if self.yolo is not None:
            r = self.yolo(img, conf=self.conf, iou=self.iou, verbose=False)[0]
            return [(*map(int, b), float(c)) for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]

        h, w = img.shape[:2]
        s = self.size
        r = min(s / w, s / h)
        nw, nh = round(w * r), round(h * r)
        px, py = (s - nw) // 2, (s - nh) // 2
        canvas = np.full((s, s, 3), 114, np.uint8)
        canvas[py:py + nh, px:px + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, swapRB=True)

        out = self.session.run(None, {self.input_name: blob})[0][0]  # (5, N): cx, cy, w, h, score
        scores = out[4]
        keep = scores >= self.conf
        if not keep.any():
            return []
        cx, cy, bw, bh = out[:4, keep]
        scores = scores[keep]
        x1 = (cx - bw / 2 - px) / r
        y1 = (cy - bh / 2 - py) / r
        boxes = np.stack([x1, y1, bw / r, bh / r], axis=1)
        idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), self.conf, self.iou)
        result = []
        for i in np.array(idx).flatten():
            x, y, bw_, bh_ = boxes[i]
            result.append((int(max(0, x)), int(max(0, y)), int(min(w, x + bw_)), int(min(h, y + bh_)),
                           float(scores[i])))
        return result
