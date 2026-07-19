import csv
import math
import os

# A commonly-cited workaround for OpenCV/Windows MSMF "can't grab frame"
# errors (error -1072875772) seen with VideoCapture.read() from a background
# thread. Harmless if unrelated to a given failure; must be set before cv2 is
# imported to have any chance of taking effect.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2
import time
import threading
import json
import serial
from flask import Flask, Response, render_template, jsonify, send_from_directory, request
from datetime import datetime
import config
import pressure_ocr

app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# ─── Camera ──────────────────────────────────────────────────────────────────

class CameraReader:
    def __init__(self):
        self.frame = None
        self.lock = threading.Lock()
        self.cap = None
        self._connected = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
        self._connected = self.cap.isOpened()

    def _loop(self):
        self._open()
        while True:
            if not self._connected:
                time.sleep(5)
                self._open()
                continue
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.frame = frame
            else:
                self._connected = False

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

    def is_connected(self):
        return self._connected


camera = CameraReader()


# ─── LakeShore 336 ───────────────────────────────────────────────────────────

class LakeShoreReader:
    CSV_HEADER = [
        "timestamp",
        "input_a_K", "input_a_C", "input_a_raw_ohm", "rdgst_a",
        "setp1_K", "htr1_pct", "htrst1", "range1",
        "setp2_K", "htr2_pct", "htrst2", "range2",
    ]

    def __init__(self):
        self._latest = None
        self._lock = threading.Lock()
        self._connected = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _query(self, ser, cmd):
        ser.reset_input_buffer()
        ser.write((cmd + "\r\n").encode())
        time.sleep(0.3)
        n = ser.in_waiting
        raw = ser.read(n) if n > 0 else b""
        return bytes(b & 0x7F for b in raw).decode("ascii", errors="replace").strip()

    def _read_all(self, ser):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return [
            ts,
            self._query(ser, "KRDG? A"),
            self._query(ser, "CRDG? A"),
            self._query(ser, "SRDG? A"),
            self._query(ser, "RDGST? A"),
            self._query(ser, "SETP? 1"),
            self._query(ser, "HTR? 1"),
            self._query(ser, "HTRST? 1"),
            self._query(ser, "RANGE? 1"),
            self._query(ser, "SETP? 2"),
            self._query(ser, "HTR? 2"),
            self._query(ser, "HTRST? 2"),
            self._query(ser, "RANGE? 2"),
        ]

    def _write_csv(self, row):
        os.makedirs(config.LS336_LOG_DIR, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(config.LS336_LOG_DIR, f"lakeshore336_{date_str}.csv")
        exists = os.path.isfile(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(self.CSV_HEADER)
            writer.writerow(row)

    def _loop(self):
        ser = None
        while True:
            if not self._connected:
                try:
                    if ser:
                        ser.close()
                    ser = serial.Serial(
                        config.LS336_PORT, config.LS336_BAUD,
                        bytesize=serial.SEVENBITS,
                        parity=serial.PARITY_ODD,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=2,
                    )
                    self._connected = True
                except Exception:
                    self._connected = False
                    time.sleep(30)
                    continue
            try:
                t0 = time.time()
                row = self._read_all(ser)
                self._write_csv(row)
                with self._lock:
                    self._latest = row
                time.sleep(max(0, config.LS336_INTERVAL - (time.time() - t0)))
            except Exception:
                self._connected = False

    def get_latest(self):
        with self._lock:
            return self._latest

    def is_connected(self):
        return self._connected


ls336 = LakeShoreReader()


# ─── Pressure LCD OCR (YOLO) ─────────────────────────────────────────────────
# See YOLO_test/PRESSURE_LCD_READER.md for how this pipeline works and why.

class PressureLCDReader:
    """Shares the single physical camera with CameraReader (pulls frames via
    camera.get_frame() instead of opening its own cv2.VideoCapture) — there's
    only one camera on this machine, used for the live stream/snapshots *and*
    for reading the pump's LCD."""

    def __init__(self, camera_reader):
        self._camera = camera_reader
        self._model = pressure_ocr.PressureOCRModel()
        self._lock = threading.Lock()
        self._last_accepted_value = None
        self._jump_candidate_value = None   # tracks a repeated out-of-jump-range reading across cycles
        self._jump_candidate_count = 0       # to tell "real step change" apart from "one-off misread"
        self._last_boxes = []          # [(text, det_conf, ocr_conf, (x1,y1,x2,y2))] in full-frame coords, for overlay
        self._status = {
            "connected": False,
            "last_attempt_time": None,
            "last_attempt_value": None,
            "last_attempt_reason": None,
            "mode_confirmed": None,
            "last_accepted_value": None,
            "last_accepted_time": None,
        }
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        attempt_num = 0
        next_attempt_time = 0.0

        while True:
            connected = self._camera.is_connected()
            with self._lock:
                self._status["connected"] = connected
            if not connected:
                time.sleep(1)
                continue

            if time.time() < next_attempt_time:
                time.sleep(0.5)
                continue

            frame = self._camera.get_frame()
            if frame is None:
                time.sleep(0.5)
                continue

            if config.PRESSURE_OCR_ROI:
                rx, ry, rw, rh = config.PRESSURE_OCR_ROI
                crop = frame[ry:ry + rh, rx:rx + rw]
            else:
                rx, ry = 0, 0
                crop = frame

            attempt_num += 1
            ts = datetime.now()
            try:
                result = self._model.read_once(crop, conf=0.25)
            except Exception as e:
                result = {"value": None, "ocr_conf": 0.0, "det_conf": 0.0,
                          "mode_confirmed": False, "code_seen": False,
                          "label_seen": False, "all_texts": [f"error: {e}"], "boxes": []}

            accepted, reason = pressure_ocr.validate_reading(
                result["value"], result["ocr_conf"], result["mode_confirmed"],
                self._last_accepted_value,
                ocr_conf_min=config.PRESSURE_OCR_CONF_MIN,
                pressure_min=config.PRESSURE_OCR_MIN,
                pressure_max=config.PRESSURE_OCR_MAX,
                jump_decades=config.PRESSURE_OCR_JUMP_DECADES)

            # A real jump (pump vented/reset) looks identical to a one-off misread on the
            # first rejected reading. Tell them apart by persistence: if repeated attempts
            # keep landing on roughly the same "implausible" value, it's not noise — accept
            # it as the new baseline instead of rejecting forever.
            if not accepted and reason.startswith("implausible_jump") and result["value"] is not None:
                value = result["value"]
                if (self._jump_candidate_value is not None and value > 0
                        and abs(math.log10(value) - math.log10(self._jump_candidate_value))
                        <= config.PRESSURE_OCR_JUMP_CONFIRM_TOLERANCE):
                    self._jump_candidate_count += 1
                else:
                    self._jump_candidate_value = value
                    self._jump_candidate_count = 1
                if self._jump_candidate_count >= config.PRESSURE_OCR_JUMP_CONFIRM_COUNT:
                    accepted = True
                    reason = f"ok (jump confirmed x{self._jump_candidate_count})"
                    self._jump_candidate_value = None
                    self._jump_candidate_count = 0

            pressure_ocr.write_csv_row(config.PRESSURE_OCR_LOG_DIR, "pressure_ocr_raw", pressure_ocr.RAW_HEADER, [
                ts.strftime("%Y-%m-%d %H:%M:%S"), attempt_num, " | ".join(result["all_texts"]),
                result["value"] if result["value"] is not None else "",
                f"{result['ocr_conf']:.2f}", f"{result['det_conf']:.2f}",
                result["code_seen"], result["label_seen"],
                "accepted" if accepted else "rejected", reason,
            ])

            with self._lock:
                self._status.update({
                    "last_attempt_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_attempt_value": result["value"],
                    "last_attempt_reason": reason,
                    "mode_confirmed": result["mode_confirmed"],
                })
                # shift box coords from crop-space back to full-frame-space for overlay drawing
                self._last_boxes = [
                    (text, det_conf, ocr_conf, (x1 + rx, y1 + ry, x2 + rx, y2 + ry))
                    for text, det_conf, ocr_conf, (x1, y1, x2, y2) in result["boxes"]
                ]

            if accepted:
                self._last_accepted_value = result["value"]
                self._jump_candidate_value = None
                self._jump_candidate_count = 0
                pressure_ocr.write_csv_row(config.PRESSURE_OCR_LOG_DIR, "pressure_ocr_clean", pressure_ocr.CLEAN_HEADER,
                    [ts.strftime("%Y-%m-%d %H:%M:%S"), f"{result['value']:.3E}"])
                _append_pressure_entry(config.PRESSURE_OCR_PRESET, ts.strftime("%Y-%m-%dT%H:%M:%S"),
                                        result["value"], note="auto:yolo_ocr")
                with self._lock:
                    self._status["last_accepted_value"] = result["value"]
                    self._status["last_accepted_time"] = ts.strftime("%Y-%m-%d %H:%M:%S")
                attempt_num = 0
                next_attempt_time = time.time() + config.PRESSURE_OCR_INTERVAL
            elif attempt_num > config.PRESSURE_OCR_MAX_RETRIES:
                attempt_num = 0
                next_attempt_time = time.time() + config.PRESSURE_OCR_INTERVAL
            else:
                next_attempt_time = time.time() + config.PRESSURE_OCR_RETRY_DELAY

    def get_status(self):
        with self._lock:
            return dict(self._status)

    def reset_baseline(self):
        """Forget the last-accepted value and any pending jump-confirmation,
        so the next reading is accepted immediately regardless of how far it
        is from history. Used when starting a fresh logging session."""
        self._last_accepted_value = None
        self._jump_candidate_value = None
        self._jump_candidate_count = 0
        with self._lock:
            self._status["last_accepted_value"] = None
            self._status["last_accepted_time"] = None

    def get_overlay(self):
        """(boxes, status_line) for drawing onto the live stream. status_line
        is a short string like 'Pressure: 1.50E-03 mbar' or None."""
        with self._lock:
            boxes = list(self._last_boxes)
            value = self._status["last_accepted_value"]
        status_line = f"Pressure: {value:.3E} mbar" if value is not None else None
        return boxes, status_line


# instantiated after _append_pressure_entry()/pressure_store are defined below,
# since its background thread calls _append_pressure_entry() on accepted reads.


# ─── Runtime interval ────────────────────────────────────────────────────────

_current_interval = config.SNAPSHOT_INTERVAL
_elapsed_since_snap = 0
_interval_lock_rt = threading.Lock()


def get_interval():
    with _interval_lock_rt:
        return _current_interval


def set_interval(seconds):
    global _current_interval, _elapsed_since_snap
    with _interval_lock_rt:
        _current_interval = int(seconds)
        _elapsed_since_snap = 0   # reset countdown immediately


# ─── Snapshot metadata ────────────────────────────────────────────────────────

_META_FILE = os.path.join(config.BASE_DIR, "snapshots_meta.json")
_meta_lock = threading.Lock()


def _load_meta():
    if os.path.exists(_META_FILE):
        with open(_META_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_meta_file(data):
    with open(_META_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


snap_meta = _load_meta()


# ─── Snapshot ────────────────────────────────────────────────────────────────

def take_snapshot():
    frame = camera.get_frame()
    if frame is None:
        return None
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"snapshot_{ts}.jpg"
    cv2.imwrite(os.path.join(config.SNAPSHOT_DIR, filename), frame)
    with _meta_lock:
        snap_meta[filename] = {"interval_s": get_interval()}
        _save_meta_file(snap_meta)
    return filename


def _auto_snapshot_loop():
    global _elapsed_since_snap
    while True:
        time.sleep(1)
        with _interval_lock_rt:
            _elapsed_since_snap += 1
            fire = _elapsed_since_snap >= _current_interval
            if fire:
                _elapsed_since_snap = 0
        if fire:
            take_snapshot()


threading.Thread(target=_auto_snapshot_loop, daemon=True).start()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def list_snapshots(limit=None):
    if not os.path.exists(config.SNAPSHOT_DIR):
        return []
    files = sorted(
        [f for f in os.listdir(config.SNAPSHOT_DIR) if f.lower().endswith(".jpg")],
        reverse=True,
    )
    return files[:limit] if limit else files


def format_ts(filename):
    raw = filename.replace("snapshot_", "").replace(".jpg", "")
    try:
        return datetime.strptime(raw, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


app.jinja_env.filters["format_ts"] = format_ts


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    limit = request.args.get("limit", 10, type=int)
    recent = list_snapshots(limit)
    all_snaps = list_snapshots()
    with _meta_lock:
        meta = dict(snap_meta)
    return render_template(
        "index.html",
        recent=recent,
        all_snaps=all_snaps,
        limit=limit,
        total=len(all_snaps),
        camera_ok=camera.is_connected(),
        interval=get_interval(),
        snap_meta=meta,
    )


def _draw_pressure_overlay(frame):
    boxes, status_line = pressure_lcd_reader.get_overlay()
    for text, det_conf, ocr_conf, (x1, y1, x2, y2) in boxes:
        is_pressure = pressure_ocr.parse_pressure(text) is not None
        color = (0, 255, 0) if is_pressure else (0, 200, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, text, (x1, max(0, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    if status_line:
        cv2.putText(frame, status_line, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    return frame


def _gen_mjpeg():
    while True:
        frame = camera.get_frame()
        if frame is not None:
            frame = _draw_pressure_overlay(frame)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + buf.tobytes()
                    + b"\r\n"
                )
                time.sleep(1 / 25)
                continue
        time.sleep(0.5)


@app.route("/video_feed")
def video_feed():
    return Response(_gen_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/snapshot", methods=["POST"])
def manual_snapshot():
    name = take_snapshot()
    if name:
        return jsonify({"ok": True, "filename": name})
    return jsonify({"ok": False, "error": "Camera unavailable"}), 503


@app.route("/snapshots/<path:filename>")
def serve_snapshot(filename):
    return send_from_directory(config.SNAPSHOT_DIR, filename)


@app.route("/latest")
def latest():
    snaps = list_snapshots(1)
    if snaps:
        return send_from_directory(config.SNAPSHOT_DIR, snaps[0])
    return "No snapshots yet", 404


@app.route("/api/snapshots")
def api_snapshots():
    limit = request.args.get("limit", None, type=int)
    base = request.host_url.rstrip("/")
    result = [
        {
            "filename": f,
            "url": f"{base}/snapshots/{f}",
            "timestamp": format_ts(f),
        }
        for f in list_snapshots(limit)
    ]
    return jsonify(result)


@app.route("/api/status")
def api_status():
    snaps = list_snapshots(1)
    return jsonify(
        {
            "camera": "online" if camera.is_connected() else "offline",
            "snapshot_interval_s": config.SNAPSHOT_INTERVAL,
            "total_snapshots": len(list_snapshots()),
            "latest": snaps[0] if snaps else None,
            "latest_url": f"{request.host_url.rstrip('/')}/latest" if snaps else None,
        }
    )


# ─── Snapshot by index (0=latest, 1=second latest, ...) ─────────────────────

@app.route("/snapshot/n/<int:n>")
def snapshot_nth(n):
    snaps = list_snapshots()
    if n < len(snaps):
        return send_from_directory(config.SNAPSHOT_DIR, snaps[n])
    return "", 204


# ─── Pressure Data ───────────────────────────────────────────────────────────

_PRESSURE_FILE = os.path.join(config.BASE_DIR, "pressure_data.json")
_pressure_lock = threading.Lock()


def _load_pressure():
    if os.path.exists(_PRESSURE_FILE):
        with open(_PRESSURE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_pressure(data):
    with open(_PRESSURE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


pressure_store = _load_pressure()
pressure_store.setdefault(config.PRESSURE_OCR_PRESET, [])


def _append_pressure_entry(preset, time_str, value, cryostat=None, speed=None, current=None, note=""):
    entry = {
        "time":     time_str,
        "value":    value,
        "cryostat": cryostat,
        "speed":    speed,
        "current":  current,
        "note":     note,
    }
    with _pressure_lock:
        pressure_store.setdefault(preset, []).append(entry)
        _save_pressure(pressure_store)


pressure_lcd_reader = PressureLCDReader(camera)


@app.route("/api/pressure", methods=["GET"])
def get_pressure():
    preset = request.args.get("preset", "default")
    with _pressure_lock:
        return jsonify(pressure_store.get(preset, []))


@app.route("/api/pressure", methods=["POST"])
def add_pressure():
    body = request.get_json()
    preset = body.get("preset", "default")
    try:
        value = float(body["value"])
    except (KeyError, ValueError):
        return jsonify({"ok": False, "error": "Invalid value"}), 400
    cryo_raw = body.get("cryostat", "")
    try:
        cryostat = float(cryo_raw) if cryo_raw != "" else None
    except (ValueError, TypeError):
        cryostat = None
    speed_raw = body.get("speed", "")
    try:
        speed = float(speed_raw) if speed_raw != "" else None
    except (ValueError, TypeError):
        speed = None
    current_raw = body.get("current", "")
    try:
        current = float(current_raw) if current_raw != "" else None
    except (ValueError, TypeError):
        current = None
    _append_pressure_entry(preset, body.get("time", ""), value, cryostat, speed, current,
                            str(body.get("note", "")).strip())
    return jsonify({"ok": True})


@app.route("/api/pressure/point", methods=["DELETE"])
def delete_pressure_point():
    body = request.get_json()
    preset = body.get("preset", "default")
    idx = body.get("index")
    with _pressure_lock:
        pts = pressure_store.get(preset, [])
        if idx is not None and 0 <= idx < len(pts):
            pts.pop(idx)
            _save_pressure(pressure_store)
    return jsonify({"ok": True})


@app.route("/api/pressure/reset", methods=["POST"])
def reset_pressure():
    preset = request.get_json().get("preset", "default")
    with _pressure_lock:
        pressure_store[preset] = []
        _save_pressure(pressure_store)
    return jsonify({"ok": True})


@app.route("/api/pressure/presets", methods=["GET"])
def list_presets():
    with _pressure_lock:
        return jsonify(sorted(pressure_store.keys()))


@app.route("/api/pressure/preset", methods=["POST"])
def create_preset():
    name = request.get_json().get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    with _pressure_lock:
        pressure_store.setdefault(name, [])
        _save_pressure(pressure_store)
    return jsonify({"ok": True})


@app.route("/api/pressure/preset/rename", methods=["POST"])
def rename_preset():
    body = request.get_json()
    old = body.get("old_name", "").strip()
    new = body.get("new_name", "").strip()
    if not old or not new:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if old == new:
        return jsonify({"ok": True})
    with _pressure_lock:
        if old not in pressure_store:
            return jsonify({"ok": False, "error": "Preset not found"}), 404
        if new in pressure_store:
            return jsonify({"ok": False, "error": "Name already exists"}), 409
        pressure_store[new] = pressure_store.pop(old)
        _save_pressure(pressure_store)
    return jsonify({"ok": True})


@app.route("/api/pressure/preset", methods=["DELETE"])
def delete_preset():
    name = request.get_json().get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name required"}), 400
    if name == "default":
        return jsonify({"ok": False, "error": "Cannot delete default preset"}), 400
    with _pressure_lock:
        pressure_store.pop(name, None)
        _save_pressure(pressure_store)
    return jsonify({"ok": True})


# ─── Interval config API ─────────────────────────────────────────────────────

@app.route("/api/config/interval", methods=["GET"])
def get_interval_api():
    iv = get_interval()
    with _interval_lock_rt:
        elapsed = _elapsed_since_snap
    return jsonify({"interval_s": iv, "next_in": max(0, iv - elapsed)})


@app.route("/api/config/interval", methods=["POST"])
def set_interval_api():
    body = request.get_json()
    try:
        seconds = int(body["interval_s"])
        if seconds < 600:
            return jsonify({"ok": False, "error": "Minimum 10 min (pressure OCR now covers frequent monitoring)"}), 400
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "Invalid value"}), 400
    set_interval(seconds)
    return jsonify({"ok": True, "interval_s": seconds})


@app.route("/api/snapshots/meta")
def get_snapshots_meta():
    with _meta_lock:
        return jsonify(snap_meta)


# ─── Grafana Embed Pages ──────────────────────────────────────────────────────

@app.route("/embed/stream")
def embed_stream():
    return render_template("embed_stream.html", camera_ok=camera.is_connected())


@app.route("/embed/snapshot")
def embed_snapshot():
    snaps = list_snapshots(1)
    return render_template(
        "embed_snapshot.html",
        has_snap=len(snaps) > 0,
        interval_ms=get_interval() * 1000,
    )


@app.route("/embed/gallery")
def embed_gallery():
    limit = request.args.get("limit", 10, type=int)
    snaps = list_snapshots(limit)
    return render_template("embed_gallery.html", snaps=snaps, limit=limit)


# ─── LakeShore 336 API ───────────────────────────────────────────────────────

@app.route("/api/temperature/latest")
def temperature_latest():
    row = ls336.get_latest()
    connected = ls336.is_connected()
    if row is None:
        return jsonify({"status": "disconnected" if not connected else "connected",
                        "timestamp": None, "input_a_K": None,
                        "htr1_pct": None, "htr2_pct": None})
    ts, a_K, a_C, a_ohm, rdgst, setp1, htr1, htrst1, range1, setp2, htr2, htrst2, range2 = row
    return jsonify({
        "status": "connected" if connected else "disconnected",
        "timestamp": ts,
        "input_a_K": a_K,
        "htr1_pct": htr1,
        "htr2_pct": htr2,
    })


@app.route("/api/temperature/history")
def temperature_history():
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(config.LS336_LOG_DIR, f"lakeshore336_{date_str}.csv")
    if not os.path.exists(path):
        return jsonify([])
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "timestamp": row.get("timestamp", ""),
                "input_a_K": row.get("input_a_K", ""),
                "htr1_pct":  row.get("htr1_pct", ""),
                "htr2_pct":  row.get("htr2_pct", ""),
            })
    return jsonify(rows)


@app.route("/embed/temperature")
def embed_temperature():
    return render_template("embed_temperature.html", ls_ok=ls336.is_connected())


# ─── Pressure LCD OCR API ────────────────────────────────────────────────────

@app.route("/api/pressure_ocr/status")
def pressure_ocr_status():
    return jsonify(pressure_lcd_reader.get_status())


@app.route("/api/pressure_ocr/new_session", methods=["POST"])
def pressure_ocr_new_session():
    """Archive whatever's currently in the live OCR preset (if non-empty) under
    a timestamped name and start a fresh empty one — no app restart needed."""
    preset = config.PRESSURE_OCR_PRESET
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived = None
    with _pressure_lock:
        existing = pressure_store.get(preset, [])
        if existing:
            archived = f"{preset}_{ts}"
            pressure_store[archived] = existing
        pressure_store[preset] = []
        _save_pressure(pressure_store)
    pressure_lcd_reader.reset_baseline()
    return jsonify({"ok": True, "archived_as": archived, "active_preset": preset})


@app.route("/embed/pressure_ocr")
def embed_pressure_ocr():
    return render_template("embed_pressure_ocr.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)
