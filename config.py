import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CAMERA_INDEX = 1                  # 0 = first camera, 1 = second, etc. — shared by both
                                   # the live stream/snapshots AND pressure OCR below (one camera, one feed)

# Manual exposure/gain/white-balance, found with YOLO_test/camera_exposure_tune.py
# against the real pump LCD. Locks these in instead of relying on the camera's
# auto-exposure/auto-WB, which real logs showed can take *hours* to converge to a
# readable picture (0% -> 37% OCR accept rate over ~3h in one session) and resets
# the moment the camera gets bumped or room lighting changes. Set any of these to
# None to leave that particular property on auto.
CAMERA_MANUAL_EXPOSURE = -5
CAMERA_GAIN = 0
CAMERA_MANUAL_WB_TEMP = 3033
CAMERA_BRIGHTNESS = 64
CAMERA_CONTRAST = 64
SNAPSHOT_INTERVAL = 600           # seconds — default (30s floor; pressure OCR below is not reliable enough
                                   # yet on real hardware to be the only frequent-monitoring source)
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
PRESSURE_OCR_ROI = None            # (x, y, w, h) fallback used only if no ROI has ever been saved from
                                   # the web UI (see pressure_ocr_roi.json, created by the "Select ROI"
                                   # control on the dashboard — drag a box on the live stream, no restart
                                   # needed). None = use the full frame (works, but less reliable).
PRESSURE_OCR_UPSCALE = 2.0         # multiply the ROI crop's size by this before YOLO+OCR see it.
                                   # Only applies once an ROI is set (upscaling a noisy full frame
                                   # doesn't help). Real hardware LCDs are dim/small — a bigger,
                                   # sharper crop measurably improves detection. Try 2.0-3.0.
PRESSURE_OCR_YOLO_CONF = 0.15      # YOLO detection confidence threshold. Lowered from 0.25 — once an
                                   # ROI narrows the field to just the LCD, weaker/dimmer detections
                                   # are worth letting through since there's little else to false-positive on.
PRESSURE_OCR_ALERT_THRESHOLD = 20  # consecutive rejected attempts before the dashboard shows a
                                   # "check the camera/ROI" warning (e.g. camera got bumped out of frame).

# Template matching (lcd_template_ocr.py) — the primary reader, with the YOLO+EasyOCR
# path above kept as a fallback. General OCR can't handle this LCD's dot-matrix font
# (it slashes its zeros; logs showed "0" read as l/i/]/Z/I/O and "3" as S), so glyphs
# are matched against bitmaps cut from this exact display instead. Set up the
# templates with: python YOLO_test/build_lcd_templates.py --image <a snapshot>
PRESSURE_OCR_USE_TEMPLATE = True
PRESSURE_OCR_TEMPLATE_MIN_SCORE = 0.70  # reject the reading if any character of the number
                                        # scores below this. Measured on real snapshots:
                                        # correct glyphs land at 0.73-0.91, while a glyph with
                                        # no template (matched to the nearest wrong one) fell to
                                        # 0.35-0.61 — so this cleanly separates the two.
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
