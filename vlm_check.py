"""Try the VLM on its own, without running the web app.

  python vlm_check.py                      list the models the server offers
  python vlm_check.py snapshots/foo.jpg    read that image (whole image, or --roi x y w h)

Run this first when setting up: it shows whether the key, URL, and model name work,
and what the model actually says about a real snapshot.
"""
import argparse

import cv2

import config
import pressure_ocr
import vlm_ocr

ap = argparse.ArgumentParser()
ap.add_argument("image", nargs="?")
ap.add_argument("--roi", nargs=4, type=int, metavar=("X", "Y", "W", "H"))
args = ap.parse_args()

reader = vlm_ocr.VLMReader()
if not args.image:
    print("\n".join(reader.list_models()))
else:
    img = cv2.imread(args.image)
    if img is None:
        raise SystemExit(f"cannot read {args.image}")
    if args.roi:
        x, y, w, h = args.roi
        img = img[y:y + h, x:x + w]
    text = reader.read_text(img, config.PRESSURE_OCR_UPSCALE)
    print(f"model: {reader.model}\n--- reply ---\n{text}\n-------------")
    print("parsed pressure:", pressure_ocr.parse_pressure(text.replace("\n", " ")))
    print("mode (340/Pressure) seen:", pressure_ocr.check_mode(text.splitlines()))
