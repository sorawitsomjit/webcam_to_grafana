import argparse
import csv
import math
import os
import re
import threading
import time
import warnings
from datetime import datetime

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['YOLO_VERBOSE'] = 'False'
os.environ['MPLBACKEND'] = 'Agg'
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
warnings.filterwarnings('ignore')

import cv2
from huggingface_hub import hf_hub_download
from ultralytics import YOLO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')

# e.g. "6.3E-6" — single-digit mantissa (this LCD's fixed "%.1E" format).
# Separator is optional/loose because OCR sometimes misreads the dot as a
# space or underscore.
PRESSURE_RE = re.compile(r'(\d)[._]?(\d)\s*[eE]\s*([+-]?\d+)')

# Pfeiffer-style vacuum controllers show "<param code>: <name>" alongside the
# reading (this LCD mockup mimics that: "340: Pressure"). Param code 340 is
# consistently read at conf 0.94-1.00 in testing, more reliable than the
# word "Pressure" which OCR sometimes mangles ("Pressurc") — so either one
# confirming is treated as enough, but both are logged for visibility.
PRESSURE_CODE_RE = re.compile(r'^340:?$')

GUI_AVAILABLE = True
try:
    cv2.namedWindow('_test', cv2.WINDOW_NORMAL)
    cv2.destroyWindow('_test')
except cv2.error:
    GUI_AVAILABLE = False

TEXT_MODEL_PATH = hf_hub_download('RoyRud1902/yolo11n-text', 'best.pt')
TEXT_MODEL = YOLO(TEXT_MODEL_PATH)

reader = None

det_results = []          # list of (text, det_conf, ocr_conf, (x1,y1,x2,y2)) — for preview
det_pressure = None       # last parsed value this attempt, regardless of accept/reject
det_status = ''           # short status string for preview overlay
det_lock = threading.Lock()
det_thread = None

last_accepted_value = None
last_accepted_time = None


def get_ocr_reader():
    global reader
    if reader is None:
        import easyocr
        reader = easyocr.Reader(['en'], gpu=False)
    return reader


def parse_pressure(text):
    m = PRESSURE_RE.search(text.replace(' ', ''))
    if not m:
        return None
    ones, tenths, exponent = m.groups()
    return float(f"{ones}.{tenths}") * (10 ** int(exponent))


def check_mode(texts):
    code_seen = any(PRESSURE_CODE_RE.match(t.strip()) for t in texts)
    label_seen = any(t.strip().lower().startswith('pressur') for t in texts)
    return code_seen, label_seen


def validate_reading(value, ocr_conf, mode_confirmed, last_value, args):
    if value is None:
        return False, 'no_number_found'
    if ocr_conf < args.ocr_conf_min:
        return False, f'low_ocr_conf({ocr_conf:.2f})'
    if not (args.pressure_min <= value <= args.pressure_max):
        return False, 'out_of_range'
    if not mode_confirmed:
        return False, 'mode_not_confirmed'
    if last_value is not None and last_value > 0 and value > 0:
        jump = abs(math.log10(value) - math.log10(last_value))
        if jump > args.jump_decades:
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


def write_csv_row(filename_prefix, header, row):
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(LOG_DIR, f'{filename_prefix}_{date_str}.csv')
    exists = os.path.isfile(path)
    with open(path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(header)
        writer.writerow(row)


RAW_HEADER = ['timestamp', 'attempt', 'raw_texts', 'parsed_value',
              'ocr_conf', 'det_conf', 'code_seen', 'label_seen',
              'decision', 'reason']
CLEAN_HEADER = ['timestamp', 'pressure_mbar']


def post_to_webapp(args, ts_iso, value):
    try:
        import requests
        requests.post(args.api_url, json={
            'preset': args.preset,
            'time': ts_iso,
            'value': value,
            'cryostat': '', 'speed': '', 'current': '',
            'note': 'auto:yolo_ocr',
        }, timeout=5)
    except Exception as e:
        print(f"    POST to webapp failed: {e}")


def attempt_reading(frame, conf, attempt_num, args):
    """One detect+OCR+validate pass. Returns (accepted, value, reason)."""
    global last_accepted_value, last_accepted_time, det_results, det_pressure, det_status

    results = TEXT_MODEL(frame, verbose=False, conf=conf)
    ocr = get_ocr_reader()
    raw_boxes = [(*map(int, box.xyxy[0]), float(box.conf[0])) for box in results[0].boxes]
    merged_boxes = merge_nearby_boxes(raw_boxes)

    boxes_data = []
    all_texts = []
    best = None  # (value, ocr_conf, det_conf)
    for x1, y1, x2, y2, det_conf in merged_boxes:
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        hits = ocr.readtext(crop)
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
    mode_confirmed = code_seen or label_seen
    value, ocr_conf, det_conf = best if best else (None, 0.0, 0.0)

    accepted, reason = validate_reading(value, ocr_conf, mode_confirmed, last_accepted_value, args)

    ts = datetime.now()
    ts_str = ts.strftime('%H:%M:%S')
    print(f"[{ts_str}] Attempt {attempt_num}: value={value}  mode(code={code_seen},label={label_seen})  -> {'ACCEPTED' if accepted else 'rejected: ' + reason}")

    write_csv_row('pressure_raw', RAW_HEADER, [
        ts.strftime('%Y-%m-%d %H:%M:%S'), attempt_num, ' | '.join(all_texts),
        value if value is not None else '', f'{ocr_conf:.2f}', f'{det_conf:.2f}',
        code_seen, label_seen, 'accepted' if accepted else 'rejected', reason,
    ])

    if accepted:
        last_accepted_value = value
        last_accepted_time = ts
        write_csv_row('pressure_clean', CLEAN_HEADER, [ts.strftime('%Y-%m-%d %H:%M:%S'), f'{value:.3E}'])
        if args.post:
            post_to_webapp(args, ts.strftime('%Y-%m-%dT%H:%M:%S'), value)

    with det_lock:
        det_results = boxes_data
        det_pressure = value
        det_status = f"attempt {attempt_num}: {'accepted' if accepted else reason}"

    return accepted, value, reason


def main():
    global det_thread

    parser = argparse.ArgumentParser(description='Read pressure value off an LCD via YOLO text-detect + OCR')
    parser.add_argument('-i', '--interval', type=float, default=10.0,
                        help='Reading interval in seconds (default: 10.0)')
    parser.add_argument('-c', '--camera', type=int, default=1,
                        help='Camera device index (default: 1)')
    parser.add_argument('--conf', type=float, default=0.25,
                        help='YOLO detection confidence threshold (default: 0.25)')
    parser.add_argument('--headless', action='store_true',
                        help='Run without camera preview window')
    parser.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'),
                        help='Skip interactive ROI selection and use this region directly')
    parser.add_argument('--ocr-conf-min', type=float, default=0.5,
                        help='Reject readings whose OCR confidence is below this (default: 0.5)')
    parser.add_argument('--pressure-min', type=float, default=1e-9,
                        help='Reject readings below this (mbar, default: 1e-9)')
    parser.add_argument('--pressure-max', type=float, default=1100.0,
                        help='Reject readings above this (mbar, default: 1100 — above atmospheric)')
    parser.add_argument('--jump-decades', type=float, default=3.0,
                        help='Reject/retry if the reading jumps more than this many orders of magnitude vs the last accepted value (default: 3.0)')
    parser.add_argument('--max-retries', type=int, default=2,
                        help='Extra attempts within the same cycle if a reading is rejected (default: 2)')
    parser.add_argument('--retry-delay', type=float, default=2.0,
                        help='Seconds to wait before a retry attempt (default: 2.0)')
    parser.add_argument('--post', action='store_true',
                        help='POST accepted readings to the webapp (default: dry-run, local CSV only)')
    parser.add_argument('--api-url', default='http://localhost:8080/api/pressure',
                        help='Webapp pressure API endpoint (default: http://localhost:8080/api/pressure)')
    parser.add_argument('--preset', default='pressure_yolo',
                        help='Preset name to POST readings under (default: pressure_yolo, kept separate from manual entries)')
    args = parser.parse_args()

    if not args.headless and not GUI_AVAILABLE:
        print("Warning: GUI not available (headless OpenCV). Switching to headless mode.")
        args.headless = True

    print("Loaded text detection model (yolo11n-text)")
    print(f"Pressure LCD reader started (interval={args.interval}s, camera={args.camera}, post={'ON -> ' + args.api_url if args.post else 'OFF (dry-run)'})")
    print("Press Ctrl+C or 'q' to stop.\n")

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"Error: Cannot open camera index {args.camera}")
        return

    if args.roi:
        rx, ry, rw, rh = args.roi
    elif not args.headless:
        print("Drag a box around the LCD readout, then press ENTER (or SPACE). Press 'c' to cancel and use the full frame.")
        ret, frame = cap.read()
        while not ret:
            ret, frame = cap.read()
        rx, ry, rw, rh = cv2.selectROI('Select ROI - press ENTER', frame, showCrosshair=True)
        cv2.destroyWindow('Select ROI - press ENTER')
        if rw == 0 or rh == 0:
            rx, ry, rw, rh = 0, 0, frame.shape[1], frame.shape[0]
        print(f"ROI = ({rx}, {ry}, {rw}, {rh})\n")
    else:
        ret, frame = cap.read()
        while not ret:
            ret, frame = cap.read()
        rx, ry, rw, rh = 0, 0, frame.shape[1], frame.shape[0]

    next_attempt_time = 0.0
    attempt_num = 0
    pending_result = None  # set by the worker thread when it finishes

    def worker(frame_copy, n):
        nonlocal pending_result
        accepted, value, reason = attempt_reading(frame_copy, args.conf, n, args)
        pending_result = (accepted, value, reason)

    try:
        while True:
            ret, full_frame = cap.read()
            if not ret:
                print("Error: Failed to capture frame")
                break
            frame = full_frame[ry:ry + rh, rx:rx + rw]

            now = time.time()
            if (det_thread is None or not det_thread.is_alive()) and pending_result is None and now >= next_attempt_time:
                attempt_num += 1
                det_thread = threading.Thread(target=worker, args=(frame.copy(), attempt_num))
                det_thread.daemon = True
                det_thread.start()

            if pending_result is not None and (det_thread is None or not det_thread.is_alive()):
                accepted, value, reason = pending_result
                pending_result = None
                if accepted or attempt_num > args.max_retries:
                    if not accepted:
                        print(f"    Giving up this cycle after {attempt_num} attempt(s): {reason}\n")
                    next_attempt_time = time.time() + args.interval
                    attempt_num = 0
                else:
                    next_attempt_time = time.time() + args.retry_delay

            if not args.headless:
                with det_lock:
                    boxes = list(det_results)
                    pressure = det_pressure
                    status = det_status
                for text, det_conf, ocr_conf, (x1, y1, x2, y2) in boxes:
                    color = (0, 255, 0) if parse_pressure(text) is not None else (0, 200, 255)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, text, (x1, y1 - 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                busy = det_thread is not None and det_thread.is_alive()
                header = "Reading..." if busy else status
                cv2.putText(frame, header, (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                if last_accepted_value is not None:
                    cv2.putText(frame, f"Last accepted = {last_accepted_value:.3E} mbar", (10, 50),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow('Pressure LCD Reader', frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    print("\nStopped via window")
                    break

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        cap.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


if __name__ == '__main__':
    main()
