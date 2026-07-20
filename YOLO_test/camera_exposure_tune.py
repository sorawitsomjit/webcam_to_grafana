"""Standalone diagnostic tool — does NOT touch app.py or any production code.

Purpose: find manual exposure/gain/white-balance values that keep the pump's
LCD readable regardless of ambient light, instead of relying on the camera's
auto-exposure/auto-WB to "settle in" over time (which real logs show can take
hours, and resets if the camera gets bumped or the room lighting changes).

Usage:
    python YOLO_test/camera_exposure_tune.py [-c CAMERA_INDEX]

Controls:
    Trackbars   - live-adjust auto/manual exposure, exposure level, gain,
                  auto/manual white balance, WB temperature
    's'         - save current frame + print the property values to copy
                  into config.py once you're happy with the picture
    'r'         - reset every property back to its camera-default (auto) state
    'q' / Esc   - quit

Notes:
    - Property support varies a LOT by camera/driver on Windows. If a
      trackbar move does nothing to the image, this camera likely ignores
      that property over this backend — the tool prints whether each
      cap.set() call actually "stuck" (i.e. cap.get() reflects it back)
      so you don't have to guess.
    - Uses the DSHOW backend (like YOLO_test/pressure_lcd_reader.py) since
      it tends to expose manual exposure controls more reliably on Windows
      than the MSMF backend app.py currently uses by default.
"""
import argparse
import os
import sys
from datetime import datetime

import cv2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(BASE_DIR, 'exposure_test')

# trackbar <-> cv2 property value mapping. Windows/DSHOW exposure is usually
# reported on a log2 scale (e.g. -13..-1); trackbars can't go negative, so we
# store an offset and convert both ways.
EXPOSURE_OFFSET = 13   # trackbar 0..13  ->  exposure -13..0
WB_MIN, WB_MAX = 2000, 10000


def get_or_na(cap, prop):
    v = cap.get(prop)
    return v


def try_set(cap, prop, value, label):
    before = cap.get(prop)
    cap.set(prop, value)
    after = cap.get(prop)
    stuck = "OK" if abs(after - value) < 1e-3 else f"IGNORED (still {after})"
    print(f"  {label}: requested={value}  before={before}  after={after}  [{stuck}]")
    return after


def main():
    parser = argparse.ArgumentParser(description="Tune webcam exposure/gain/WB for reading a dim LCD")
    parser.add_argument('-c', '--camera', type=int, default=1, help='Camera device index (default: 1)')
    parser.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'),
                        help='Crop to this region for the preview (optional)')
    args = parser.parse_args()

    cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"Error: cannot open camera index {args.camera}")
        return

    print("Current camera property values (auto mode, before any changes):")
    print(f"  CAP_PROP_AUTO_EXPOSURE = {cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)}")
    print(f"  CAP_PROP_EXPOSURE      = {cap.get(cv2.CAP_PROP_EXPOSURE)}")
    print(f"  CAP_PROP_GAIN          = {cap.get(cv2.CAP_PROP_GAIN)}")
    print(f"  CAP_PROP_AUTO_WB       = {cap.get(cv2.CAP_PROP_AUTO_WB)}")
    print(f"  CAP_PROP_WB_TEMPERATURE= {cap.get(cv2.CAP_PROP_WB_TEMPERATURE)}")
    print(f"  CAP_PROP_BRIGHTNESS    = {cap.get(cv2.CAP_PROP_BRIGHTNESS)}")
    print(f"  CAP_PROP_CONTRAST      = {cap.get(cv2.CAP_PROP_CONTRAST)}")
    print()

    cv2.namedWindow('Exposure Tune', cv2.WINDOW_NORMAL)
    cv2.createTrackbar('AutoExp(0/1)', 'Exposure Tune', 1, 1, lambda v: None)
    cv2.createTrackbar('Exposure', 'Exposure Tune', EXPOSURE_OFFSET - 6, EXPOSURE_OFFSET, lambda v: None)
    cv2.createTrackbar('Gain', 'Exposure Tune', 0, 255, lambda v: None)
    cv2.createTrackbar('AutoWB(0/1)', 'Exposure Tune', 1, 1, lambda v: None)
    cv2.createTrackbar('WBTemp', 'Exposure Tune', 4500 - WB_MIN, WB_MAX - WB_MIN, lambda v: None)
    cv2.createTrackbar('Brightness', 'Exposure Tune', 64, 128, lambda v: None)
    cv2.createTrackbar('Contrast', 'Exposure Tune', 64, 128, lambda v: None)

    print("Drag the trackbars and watch the preview. Press 's' to save + print values, 'r' to reset to auto, 'q' to quit.\n")

    os.makedirs(SAVE_DIR, exist_ok=True)
    last_state = {}

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Error: failed to grab frame")
            break
        if args.roi:
            x, y, w, h = args.roi
            frame = frame[y:y + h, x:x + w]

        auto_exp = cv2.getTrackbarPos('AutoExp(0/1)', 'Exposure Tune')
        exposure = cv2.getTrackbarPos('Exposure', 'Exposure Tune') - EXPOSURE_OFFSET
        gain = cv2.getTrackbarPos('Gain', 'Exposure Tune')
        auto_wb = cv2.getTrackbarPos('AutoWB(0/1)', 'Exposure Tune')
        wb_temp = cv2.getTrackbarPos('WBTemp', 'Exposure Tune') + WB_MIN
        brightness = cv2.getTrackbarPos('Brightness', 'Exposure Tune') - 64
        contrast = cv2.getTrackbarPos('Contrast', 'Exposure Tune') - 64

        state = (auto_exp, exposure, gain, auto_wb, wb_temp, brightness, contrast)
        if state != last_state:
            # DSHOW convention: CAP_PROP_AUTO_EXPOSURE = 0.75 means auto, 0.25 means manual
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75 if auto_exp else 0.25)
            if not auto_exp:
                cap.set(cv2.CAP_PROP_EXPOSURE, exposure)
            cap.set(cv2.CAP_PROP_GAIN, gain)
            cap.set(cv2.CAP_PROP_AUTO_WB, auto_wb)
            if not auto_wb:
                cap.set(cv2.CAP_PROP_WB_TEMPERATURE, wb_temp)
            cap.set(cv2.CAP_PROP_BRIGHTNESS, brightness)
            cap.set(cv2.CAP_PROP_CONTRAST, contrast)
            last_state = state

        overlay = frame.copy()
        cv2.putText(overlay, f"AutoExp={auto_exp} Exp={exposure} Gain={gain} AutoWB={auto_wb} WBTemp={wb_temp}",
                    (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
        cv2.putText(overlay, "'s'=save+print values  'r'=reset to auto  'q'=quit",
                    (10, frame.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
        cv2.imshow('Exposure Tune', overlay)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key == ord('r'):
            print("Resetting to auto exposure/WB...")
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            cap.set(cv2.CAP_PROP_AUTO_WB, 1)
            cv2.setTrackbarPos('AutoExp(0/1)', 'Exposure Tune', 1)
            cv2.setTrackbarPos('AutoWB(0/1)', 'Exposure Tune', 1)
        elif key == ord('s'):
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = os.path.join(SAVE_DIR, f'exposure_test_{ts}.jpg')
            cv2.imwrite(path, frame)
            print(f"\nSaved: {path}")
            print("Copy these into config.py if this looks good (values that actually 'stuck' — see below):")
            print(f"  CAMERA_MANUAL_EXPOSURE = {'None' if auto_exp else exposure}")
            print(f"  CAMERA_GAIN            = {gain}")
            print(f"  CAMERA_MANUAL_WB_TEMP  = {'None' if auto_wb else wb_temp}")
            print(f"  CAMERA_BRIGHTNESS      = {brightness}")
            print(f"  CAMERA_CONTRAST        = {contrast}")
            print("Actual values the camera reports back (confirms whether they stuck):")
            try_set(cap, cv2.CAP_PROP_EXPOSURE, exposure, 'Exposure')
            try_set(cap, cv2.CAP_PROP_GAIN, gain, 'Gain')
            try_set(cap, cv2.CAP_PROP_WB_TEMPERATURE, wb_temp, 'WB Temp')
            print()

    cap.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
