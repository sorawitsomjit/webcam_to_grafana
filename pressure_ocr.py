import csv
import math
import os
import re
import threading
from datetime import datetime

import cv2

# e.g. "6.3E-6" — single-digit mantissa (this LCD's fixed "%.1E" format).
# Separator is optional/loose because OCR sometimes misreads the dot as a
# space or underscore (on real hardware, a plain space is the most common
# misread — e.g. "6 3E0" for "6.3E0").
#
# The exponent group also accepts a handful of characters EasyOCR substitutes
# for digits in this font. Which digit each one stands for was read off the
# logs directly, by finding confusable reads sitting next to a clean read of
# the same value moments earlier or later:
#
#   '1 4El'   next to clean '1 4E1'   -> l = 1
#   '4 0EI'   next to clean '4.0E1'   -> I = 1
#   '1 0Ei'   next to clean '1 2E1'   -> i = 1
#   '3.4E]'   next to clean '3 4E1'   -> ] = 1
#   '1 0EZ'   next to clean '1 0E2'   -> Z = 2
#   '6 3EO'   next to clean '6 3E0'   -> O = 0
#
# Getting this wrong is worse than not guessing at all: an exponent off by one
# is a reading off by 10x, and it arrives looking perfectly plausible. A first
# pass here mapped all of these to '0' on the assumption they were the font's
# slashed zero, which silently turned 4.6E1 (46 mbar) into 4.6.
PRESSURE_RE = re.compile(r'(\d)[._ ]?(\d)\s*[eE]\s*([+-]?[\dlIiZzO\]]+)')
_EXP_CONFUSABLES = str.maketrans({
    'l': '1', 'I': '1', 'i': '1', ']': '1',
    'Z': '2', 'z': '2',
    'O': '0',
})

# Pfeiffer-style vacuum controllers show "<param code>: <name>" alongside the
# reading (this LCD mockup mimics that: "340: Pressure"). On real hardware,
# YOLO's box-merge often fuses "340:" and "Pressure" into one text (e.g.
# "340 Pressure", "340 Pres") since they sit on the same line close
# together — so these must be substring searches, not exact-match, or a
# merged box confirms neither.
PRESSURE_CODE_RE = re.compile(r'340\s*:?')

RAW_HEADER = ['timestamp', 'attempt', 'raw_texts', 'parsed_value',
              'ocr_conf', 'det_conf', 'code_seen', 'label_seen',
              'decision', 'reason']
CLEAN_HEADER = ['timestamp', 'pressure_mbar']


def parse_pressure(text):
    m = PRESSURE_RE.search(text.replace(' ', ''))
    if not m:
        return None
    ones, tenths, exp_raw = m.groups()
    exponent = exp_raw.translate(_EXP_CONFUSABLES)
    if not re.fullmatch(r'[+-]?\d+', exponent):
        return None
    return float(f"{ones}.{tenths}") * (10 ** int(exponent))


def check_mode(texts):
    code_seen = any(PRESSURE_CODE_RE.search(t) for t in texts)
    label_seen = any('pressur' in t.lower() for t in texts)
    return code_seen, label_seen


def validate_reading(value, ocr_conf, mode_confirmed, last_value, *,
                      ocr_conf_min, pressure_min, pressure_max, jump_decades):
    if value is None:
        return False, 'no_number_found'
    if ocr_conf < ocr_conf_min:
        return False, f'low_ocr_conf({ocr_conf:.2f})'
    if not (pressure_min <= value <= pressure_max):
        return False, 'out_of_range'
    if not mode_confirmed:
        return False, 'mode_not_confirmed'
    if last_value is not None and last_value > 0 and value > 0:
        jump = abs(math.log10(value) - math.log10(last_value))
        if jump > jump_decades:
            return False, f'implausible_jump({jump:.1f}dec)'
    return True, 'ok'


def merge_nearby_boxes(boxes, x_gap_ratio=0.6, y_overlap_ratio=0.5):
    """Merge YOLO boxes that sit on the same line and are close together
    horizontally — a single number is sometimes split into two boxes
    (e.g. "3" and "5E-5" instead of "3.5E-5"), which breaks OCR+regex
    if each box is read in isolation."""
    boxes = list(boxes)
    used = [False] * len(boxes)
    merged = []
    for i in range(len(boxes)):
        if used[i]:
            continue
        x1, y1, x2, y2, conf = boxes[i]
        cur = [x1, y1, x2, y2]
        cur_conf = conf
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j in range(len(boxes)):
                if used[j]:
                    continue
                bx1, by1, bx2, by2, bconf = boxes[j]
                overlap = min(cur[3], by2) - max(cur[1], by1)
                min_h = min(cur[3] - cur[1], by2 - by1)
                if min_h <= 0 or overlap / min_h < y_overlap_ratio:
                    continue
                gap = max(bx1 - cur[2], cur[0] - bx2)
                height = cur[3] - cur[1]
                if gap > x_gap_ratio * height:
                    continue
                cur[0] = min(cur[0], bx1)
                cur[1] = min(cur[1], by1)
                cur[2] = max(cur[2], bx2)
                cur[3] = max(cur[3], by2)
                cur_conf = max(cur_conf, bconf)
                used[j] = True
                changed = True
        merged.append((cur[0], cur[1], cur[2], cur[3], cur_conf))
    return merged


def enhance_lcd(crop):
    """CLAHE contrast boost on the L channel. Real pump LCDs run dim/low-contrast
    (unlike the bright monitor-based simulator this was first tuned against) —
    this makes the digits stand out more before YOLO/EasyOCR ever see them."""
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def write_csv_row(log_dir, filename_prefix, header, row):
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(log_dir, f'{filename_prefix}_{date_str}.csv')
    exists = os.path.isfile(path)
    with open(path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(header)
        writer.writerow(row)


class PressureOCRModel:
    """Lazily loads the YOLO text-detector + EasyOCR reader on first use, so
    importing this module doesn't trigger a model download/load — the
    caller decides when that cost gets paid (e.g. inside a background
    thread, not at Flask import time)."""

    def __init__(self):
        self._yolo = None
        self._ocr = None
        self._lock = threading.Lock()

    def ensure_loaded(self):
        if self._yolo is not None:
            return
        with self._lock:
            if self._yolo is not None:
                return
            os.environ.setdefault('YOLO_VERBOSE', 'False')
            os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
            from huggingface_hub import hf_hub_download
            from ultralytics import YOLO
            import easyocr
            model_path = hf_hub_download('RoyRud1902/yolo11n-text', 'best.pt')
            self._yolo = YOLO(model_path)
            self._ocr = easyocr.Reader(['en'], gpu=False)

    def read_once(self, frame, conf=0.25):
        """Run one detect+OCR pass on `frame`. Returns a dict: boxes (list of
        (text, det_conf, ocr_conf, (x1,y1,x2,y2))), all_texts, value,
        ocr_conf, det_conf, code_seen, label_seen, mode_confirmed."""
        self.ensure_loaded()
        results = self._yolo(frame, verbose=False, conf=conf)
        raw_boxes = [(*map(int, box.xyxy[0]), float(box.conf[0])) for box in results[0].boxes]
        merged_boxes = merge_nearby_boxes(raw_boxes)

        boxes_data = []
        all_texts = []
        best = None  # (value, ocr_conf, det_conf)
        for x1, y1, x2, y2, det_conf in merged_boxes:
            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            hits = self._ocr.readtext(crop)
            if not hits:
                continue
            text = ' '.join(t for _, t, _ in hits)
            ocr_conf = min(c for _, _, c in hits)
            boxes_data.append((text, det_conf, ocr_conf, (x1, y1, x2, y2)))
            all_texts.append(text)
            if best is None:
                value = parse_pressure(text)
                if value is not None:
                    best = (value, ocr_conf, det_conf)

        code_seen, label_seen = check_mode(all_texts)
        value, ocr_conf, det_conf = best if best else (None, 0.0, 0.0)
        return {
            'boxes': boxes_data,
            'all_texts': all_texts,
            'value': value,
            'ocr_conf': ocr_conf,
            'det_conf': det_conf,
            'code_seen': code_seen,
            'label_seen': label_seen,
            'mode_confirmed': code_seen or label_seen,
        }
