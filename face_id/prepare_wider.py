"""Write YOLO labels for WIDER FACE next to its images (no images are copied).

Expects dataset/WIDER_FACE/{WIDER_train,WIDER_val,wider_face_split}/ as unzipped from
https://huggingface.co/datasets/CUHK-CSE/wider_face. Produces WIDER_<split>/labels/<event>/<img>.txt,
which Ultralytics finds automatically from the images/ path.

    python -m face_id.prepare_wider
"""
from PIL import Image

from .common import WIDER_DIR

MIN_SIZE = 4  # px; smaller boxes are mostly unlabellable crowd specks


def convert(split):
    gt = WIDER_DIR / "wider_face_split" / f"wider_face_{split}_bbx_gt.txt"
    img_root = WIDER_DIR / f"WIDER_{split}" / "images"
    lbl_root = WIDER_DIR / f"WIDER_{split}" / "labels"
    lines = iter(gt.read_text().splitlines())
    n_img = n_box = 0
    for rel in lines:
        count = int(next(lines))
        # Images with no faces still carry one all-zero placeholder row.
        rows = [next(lines).split() for _ in range(max(count, 1))][:count]
        w, h = Image.open(img_root / rel).size
        out = []
        for x, y, bw, bh, _blur, _expr, _illum, invalid, _occ, _pose in (map(int, r[:10]) for r in rows):
            if invalid or bw < MIN_SIZE or bh < MIN_SIZE:
                continue
            x2, y2 = min(x + bw, w), min(y + bh, h)
            x, y = max(x, 0), max(y, 0)
            out.append(f"0 {(x + x2) / 2 / w:.6f} {(y + y2) / 2 / h:.6f} {(x2 - x) / w:.6f} {(y2 - y) / h:.6f}\n")
        dest = (lbl_root / rel).with_suffix(".txt")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("".join(out))
        n_img += 1
        n_box += len(out)
    print(f"{split}: {n_img} images, {n_box} faces -> {lbl_root}")


def main():
    for split in ("train", "val"):
        convert(split)


if __name__ == "__main__":
    main()
