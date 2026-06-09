import cv2
import os
import time
import threading
import json
from flask import Flask, Response, render_template, jsonify, send_from_directory, request
from datetime import datetime
import config

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


# ─── Snapshot ────────────────────────────────────────────────────────────────

def take_snapshot():
    frame = camera.get_frame()
    if frame is None:
        return None
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"snapshot_{ts}.jpg"
    cv2.imwrite(os.path.join(config.SNAPSHOT_DIR, filename), frame)
    return filename


def _auto_snapshot_loop():
    while True:
        time.sleep(config.SNAPSHOT_INTERVAL)
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
    return render_template(
        "index.html",
        recent=recent,
        all_snaps=all_snaps,
        limit=limit,
        total=len(all_snaps),
        camera_ok=camera.is_connected(),
        interval=config.SNAPSHOT_INTERVAL,
    )


def _gen_mjpeg():
    while True:
        frame = camera.get_frame()
        if frame is not None:
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
    entry = {
        "time":     body.get("time", ""),
        "value":    value,
        "cryostat": cryostat,
        "speed":    speed,
        "note":     str(body.get("note", "")).strip(),
    }
    with _pressure_lock:
        pressure_store.setdefault(preset, []).append(entry)
        _save_pressure(pressure_store)
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
        interval_ms=config.SNAPSHOT_INTERVAL * 1000,
    )


@app.route("/embed/gallery")
def embed_gallery():
    limit = request.args.get("limit", 10, type=int)
    snaps = list_snapshots(limit)
    return render_template("embed_gallery.html", snaps=snaps, limit=limit)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)
