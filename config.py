import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CAMERA_INDEX = 1                  # 0 = first camera, 1 = second, etc. — shared by both
                                   # the live stream/snapshots AND pressure OCR below (one camera, one feed)
SNAPSHOT_INTERVAL = 600           # seconds — 10 min floor/default (pressure OCR below now covers frequent monitoring)
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")
PORT = 8080

# LakeShore 336
LS336_PORT     = "COM10"
LS336_BAUD     = 57600
LS336_INTERVAL = 60               # seconds between readings
LS336_LOG_DIR  = os.path.join(BASE_DIR, "logs")

# Pressure LCD OCR (YOLO text-detect + EasyOCR) — see YOLO_test/PRESSURE_LCD_READER.md
# Reads frames from the same camera as above (CAMERA_INDEX) — no separate camera/index needed.
PRESSURE_OCR_INTERVAL = 20         # seconds between reading attempts
PRESSURE_OCR_ROI = None            # (x, y, w, h) — run YOLO_test/pressure_lcd_reader.py once against the
                                   # real pump's camera to find these values, then set them here.
                                   # None = use the full frame (works, but less reliable — see doc).
PRESSURE_OCR_PRESET = "pressure_yolo"   # kept separate from manually-entered presets
PRESSURE_OCR_CONF_MIN = 0.5
PRESSURE_OCR_MIN = 1e-9            # sanity bounds, mbar
PRESSURE_OCR_MAX = 1100.0          # above atmospheric, covers pump-down from ambient
PRESSURE_OCR_JUMP_DECADES = 3.0    # reject/retry if a reading jumps more than this many orders of magnitude
PRESSURE_OCR_JUMP_CONFIRM_COUNT = 3       # accept a jump anyway if this many attempts in a row agree
PRESSURE_OCR_JUMP_CONFIRM_TOLERANCE = 0.3  # how close (in decades) those repeats need to be to "agree"
PRESSURE_OCR_MAX_RETRIES = 2       # extra attempts per cycle after a rejection
PRESSURE_OCR_RETRY_DELAY = 2.0     # seconds before a retry attempt
PRESSURE_OCR_LOG_DIR = os.path.join(BASE_DIR, "logs")
