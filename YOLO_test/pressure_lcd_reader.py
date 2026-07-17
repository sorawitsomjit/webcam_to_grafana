import argparse
import os
import sys
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pressure_ocr import (  # noqa: E402
    PressureOCRModel, parse_pressure, validate_reading,
    write_csv_row, RAW_HEADER, CLEAN_HEADER,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')

GUI_AVAILABLE = True
try:
    cv2.namedWindow('_test', cv2.WINDOW_NORMAL)
    cv2.destroyWindow('_test')
except cv2.error:
    GUI_AVAILABLE = False

model = PressureOCRModel()

det_results = []          # list of (text, det_conf, ocr_conf, (x1,y1,x2,y2)) — for preview
det_pressure = None       # last parsed value this attempt, regardless of accept/reject
det_status = ''           # short status string for preview overlay
det_lock = threading.Lock()
det_thread = None

last_accepted_value = None


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
    global last_accepted_value, det_results, det_pressure, det_status

    result = model.read_once(frame, conf)
    value, ocr_conf = result['value'], result['ocr_conf']

    accepted, reason = validate_reading(
        value, ocr_conf, result['mode_confirmed'], last_accepted_value,
        ocr_conf_min=args.ocr_conf_min, pressure_min=args.pressure_min,
        pressure_max=args.pressure_max, jump_decades=args.jump_decades)

    ts = datetime.now()
    ts_str = ts.strftime('%H:%M:%S')
    print(f"[{ts_str}] Attempt {attempt_num}: value={value}  "
          f"mode(code={result['code_seen']},label={result['label_seen']})  "
          f"-> {'ACCEPTED' if accepted else 'rejected: ' + reason}")

    write_csv_row(LOG_DIR, 'pressure_raw', RAW_HEADER, [
        ts.strftime('%Y-%m-%d %H:%M:%S'), attempt_num, ' | '.join(result['all_texts']),
        value if value is not None else '', f'{ocr_conf:.2f}', f"{result['det_conf']:.2f}",
        result['code_seen'], result['label_seen'], 'accepted' if accepted else 'rejected', reason,
    ])

    if accepted:
        last_accepted_value = value
        write_csv_row(LOG_DIR, 'pressure_clean', CLEAN_HEADER, [ts.strftime('%Y-%m-%d %H:%M:%S'), f'{value:.3E}'])
        if args.post:
            post_to_webapp(args, ts.strftime('%Y-%m-%dT%H:%M:%S'), value)

    with det_lock:
        det_results = result['boxes']
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

    print("Loading text detection model (yolo11n-text)...")
    model.ensure_loaded()
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
