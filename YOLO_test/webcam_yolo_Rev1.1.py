import argparse
import os
import threading
import time
import warnings

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['YOLO_VERBOSE'] = 'False'
os.environ['MPLBACKEND'] = 'Agg'
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
warnings.filterwarnings('ignore')

import cv2
from huggingface_hub import hf_hub_download
from ultralytics import YOLO

GUI_AVAILABLE = True
try:
    cv2.namedWindow('_test', cv2.WINDOW_NORMAL)
    cv2.destroyWindow('_test')
except cv2.error:
    GUI_AVAILABLE = False

TEXT_MODEL_PATH = hf_hub_download('RoyRud1902/yolo11n-text', 'best.pt')
TEXT_MODEL = YOLO(TEXT_MODEL_PATH)

reader = None

det_results = []
det_lock = threading.Lock()
det_thread = None


def run_ocr_on_region(frame, x1, y1, x2, y2):
    global reader
    if reader is None:
        import easyocr
        reader = easyocr.Reader(['en'], gpu=False)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return ''
    results = reader.readtext(crop)
    if results:
        return results[0][1]
    return ''


def run_detection(frame, recognize):
    global det_results
    try:
        results = TEXT_MODEL(frame, verbose=False)
        boxes_data = []
        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            text_label = 'text'
            if recognize:
                text_label = run_ocr_on_region(frame, x1, y1, x2, y2)
                if not text_label:
                    text_label = 'text'
            boxes_data.append((cls_id, conf, (x1, y1, x2, y2), text_label))
        with det_lock:
            det_results = boxes_data
    except Exception as e:
        with det_lock:
            det_results = []
        print(f"\nDetection error: {e}")


def main():
    global det_thread

    parser = argparse.ArgumentParser(description='Webcam YOLO - detect text & numbers via webcam')
    parser.add_argument('-i', '--interval', type=float, default=10.0,
                        help='Detection interval in seconds (default: 10.0)')
    parser.add_argument('-c', '--camera', type=int, default=1,
                        help='Camera device index (default: 1)')
    parser.add_argument('--headless', action='store_true',
                        help='Run without camera preview window')
    parser.add_argument('--recognize', action='store_true',
                        help='Run OCR on detected text regions to read the text')
    parser.add_argument('--conf', type=float, default=0.25,
                        help='Confidence threshold (default: 0.25)')
    args = parser.parse_args()

    if not args.headless and not GUI_AVAILABLE:
        print("Warning: GUI not available (headless OpenCV). Switching to headless mode.")
        print("Install opencv-python (not headless) for the preview window.\n")
        args.headless = True

    print(f"Loaded text detection model (yolo11n-text)")
    if args.recognize:
        print("OCR recognition enabled (reading text from regions)")

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"Error: Cannot open camera index {args.camera}")
        return

    print(f"Webcam YOLO text detection started (interval={args.interval}s, camera={args.camera})")
    print("Press Ctrl+C or 'q' to stop.\n")

    last_capture = 0.0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Error: Failed to capture frame")
                break

            now = time.time()
            if (det_thread is None or not det_thread.is_alive()) and now - last_capture >= args.interval:
                last_capture = now
                print(f"[{time.strftime('%H:%M:%S')}] Detecting...")
                with det_lock:
                    det_results.clear()
                det_thread = threading.Thread(
                    target=run_detection, args=(frame.copy(), args.recognize))
                det_thread.daemon = True
                det_thread.start()

            elif det_thread is not None and not det_thread.is_alive():
                det_thread.join()
                with det_lock:
                    results = list(det_results)
                if results:
                    for cls_id, conf, _, text_label in results:
                        label = text_label if args.recognize else 'text'
                        print(f"[{time.strftime('%H:%M:%S')}] {label}  (conf: {conf:.2f})")
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] No text detected")
                det_thread = None

            with det_lock:
                results = list(det_results)

            if not args.headless:
                for cls_id, conf, (x1, y1, x2, y2), text_label in results:
                    color = (0, 255, 0) if conf > 0.5 else (0, 200, 255)
                    label = f"{text_label} {conf:.2f}" if args.recognize else f"text {conf:.2f}"
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, label, (x1, y1 - 5),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                busy = det_thread is not None and det_thread.is_alive()
                info = "Detecting..." if busy else f"interval={args.interval}s"
                cv2.putText(frame, info, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                cv2.imshow('Webcam YOLO Text Detection', frame)
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
