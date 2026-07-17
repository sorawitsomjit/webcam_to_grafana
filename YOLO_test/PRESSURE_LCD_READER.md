# Pressure LCD OCR

Reads the pressure value off a vacuum pump's LCD readout using a camera,
without needing any electrical/serial connection to the pump. Detects text
regions with a YOLO text-detector, reads them with EasyOCR, parses out the
`X.XE±N` pressure number, and only accepts it if it passes a few sanity
checks. Rejected readings trigger an immediate retry instead of waiting for
the next scheduled interval.

Branch: `yolo-extract-value`. Status: tested against
`vacuum_pumpdown_simulator_V3.py` (a Tkinter LCD mockup), not yet against a
physical pump. **Integrated into `app.py`** as a background reader
(`PressureLCDReader`), alongside the original standalone CLI tool used for
calibration/testing.

## Why this exists

The vacuum pressure panel in the webapp (`/api/pressure`) used to require
someone to read the pump's LCD and type the value in by hand. This
auto-logs (and now auto-posts, in-process) that reading instead: point a
camera at the pump's display and it takes care of the rest.

## Two entry points, one shared core

The detect/OCR/validate logic lives in **`pressure_ocr.py`** (project root)
so it isn't duplicated between the two places that use it:

- **`YOLO_test/pressure_lcd_reader.py`** — standalone CLI tool. Has its own
  camera loop, interactive ROI selector, live preview window, and `--post`
  flag to test against a running webapp over HTTP. Use this to calibrate a
  new camera position (it prints the ROI coordinates) and to sanity-check
  accuracy before trusting a change.
- **`app.py` → `PressureLCDReader` class** — the production path. Runs as a
  background thread started at Flask startup (same pattern as
  `CameraReader`/`LakeShoreReader`), reads `config.PRESSURE_OCR_*` settings
  instead of CLI args, and appends accepted readings directly into
  `pressure_store` in-process (no HTTP round-trip needed since it's the same
  process) via `_append_pressure_entry()`.

Both call into `pressure_ocr.PressureOCRModel.read_once()` for the actual
detect+OCR pass, and share `parse_pressure()`, `check_mode()`,
`validate_reading()`, `merge_nearby_boxes()`, and `write_csv_row()`.

## Pipeline

```
camera frame → crop to ROI → YOLO detect text boxes → merge nearby boxes
  → EasyOCR per box → regex-parse pressure value → validate → accept/reject
  → write raw CSV (always) → write clean CSV + record into pressure_store
    (if accepted; via direct append in app.py, or --post/HTTP in the CLI tool)
```

### 1. ROI (region of interest)

In the standalone tool, a window pops up on startup (unless `--roi` is
given) and you drag a box around just the LCD readout, then press ENTER —
it prints the `(x, y, w, h)` you selected. Everything outside that box is
cropped away before detection ever runs.

This matters more than it sounds: in testing, running YOLO on the *full*
camera frame (e.g. a monitor showing the simulator window next to VSCode)
caused the box-merge step (see below) to merge unrelated on-screen text
across the whole width of the frame, occasionally swallowing the actual
pressure reading into a false merge. Cropping to just the LCD removes that
failure mode entirely, and also mirrors how the camera will actually be
mounted on a real pump (aimed tightly at the display, nothing else in frame).

**In `app.py`**, there's no interactive prompt — a long-running Flask
process can't block on a GUI window at startup. Instead: run the standalone
tool once against the real camera to find the ROI, then set it as
`config.PRESSURE_OCR_ROI = (x, y, w, h)`. Leaving it as `None` falls back to
the full frame (works, but loses the reliability benefit above).

### 2. Box merging

YOLO sometimes splits a single number into two detection boxes — e.g. `"3"`
and `"5E-5"` instead of `"3.5E-5"`, or `"1"` and `"6E-3"`. Read in isolation,
neither half matches the pressure regex. `merge_nearby_boxes()` merges boxes
that are on the same line (vertical overlap) and close together horizontally
(gap less than `0.6 ×` box height) before OCR ever runs on them.

### 3. OCR + parsing

Each merged box goes through EasyOCR once. The combined text is matched
against:

```python
PRESSURE_RE = re.compile(r'(\d)[._]?(\d)\s*[eE]\s*([+-]?\d+)')
```

The separator between the two mantissa digits is optional and matches `.`,
`_`, or nothing, because OCR has been observed misreading the decimal point
as a space (stripped before matching) or an underscore. It does **not** yet
handle every OCR confusion seen — e.g. `E` misread as `F` (`"2.8F-4"`) is not
parsed; that case is instead caught by the retry mechanism (see below) since
attempt 2 on a fresh frame usually reads it correctly.

### 4. Mode confirmation

Real pump controllers (this one mimics a Pfeiffer-style display) cycle
through multiple readouts — pressure, rotor speed, drive current — each
shown as `"<param code>: <name>"` (e.g. `"340: Pressure"`). Nothing stops the
camera from reading a number while the display happens to be on a *different*
screen, which would silently log a speed or current value as if it were
pressure.

Every reading requires **at least one** of these to also be present in the
same frame:

- the literal code `"340"` (or `"340:"`) — observed to OCR very reliably
  (conf 0.94–1.00 across all tests, more reliable than the word itself)
- a text box whose lowercased text starts with `"pressur"` — a prefix check
  chosen because it matches both `"Pressure"` and OCR's common typo
  `"Pressurc"`

If neither is seen, the reading is rejected with reason `mode_not_confirmed`
rather than assumed to be pressure. When speed/current gauges are added
later, the same pattern (code + keyword) can identify which screen is
showing and route the number to the right field instead of guessing.

### 5. Validation / sanity checks (`validate_reading`)

A parsed number is only **accepted** if all of these pass:

| Check | Condition | Rejection reason |
|---|---|---|
| Found at all | a box matched `PRESSURE_RE` | `no_number_found` |
| OCR confidence | `ocr_conf >= --ocr-conf-min` (default 0.5) | `low_ocr_conf(x.xx)` |
| Physical range | `--pressure-min <= value <= --pressure-max` (default 1e-9 to 1100 mbar) | `out_of_range` |
| Mode confirmed | code or label seen (section 4) | `mode_not_confirmed` |
| Plausible jump | `\|log10(value) - log10(last_accepted)\| <= --jump-decades` (default 3.0) | `implausible_jump(x.xdec)` |

The jump check only applies once there's a prior accepted value — the first
successful reading of a run has nothing to compare against.

These bounds/thresholds were picked from what was actually observed in
testing, not derived formally — they're CLI flags specifically so they can
be tuned once tested against the real pump instead of the simulator.

### 6. Retry-on-reject

If a reading is rejected, the **very next frame** is retried after
`--retry-delay` seconds (default 2s) instead of waiting the full
`--interval`. Up to `--max-retries` (default 2) extra attempts happen within
the same cycle. This is the main defense against two failure modes that are
hard to fix any other way:

- a one-off OCR misread (e.g. the `E`→`F` case above)
- reading the display at the exact instant its digits are mid-update
  ("torn read" — the camera has no way to synchronize with the pump's
  display refresh)

If every attempt in a cycle is rejected, nothing is logged as "accepted" —
the last known-good value simply carries forward, and the cycle's failure is
still recorded in the raw log for later review.

### 7. Logging

Two CSVs per day (both gitignored — runtime data, not source):

- **`<prefix>_raw_YYYY-MM-DD.csv`** — every attempt, accepted or not.
  Columns: `timestamp, attempt, raw_texts, parsed_value, ocr_conf, det_conf,
  code_seen, label_seen, decision, reason`. This is the debugging/tuning
  trail — if accuracy needs adjusting later, this file has everything
  needed to see why a reading was accepted or rejected.
- **`<prefix>_clean_YYYY-MM-DD.csv`** — only accepted readings.
  Columns: `timestamp, pressure_mbar`.

The standalone tool writes to `YOLO_test/logs/pressure_raw_*` /
`pressure_clean_*`. `app.py`'s integrated reader writes to
`logs/pressure_ocr_raw_*` / `pressure_ocr_clean_*` (path from
`config.PRESSURE_OCR_LOG_DIR`) — kept separate so test-tool runs don't mix
with what the running webapp actually logged.

Example raw log row showing a retry recovering from a misread:

```
2026-07-17 14:14:15,1,"mbar | 340: | 2 .8F-4 | Pressure",,0.00,0.00,True,True,rejected,no_number_found
2026-07-17 14:14:18,2,"mbar | 340: | 2.9E-4 | Pressure",0.00029,0.99,0.89,True,True,accepted,ok
```

### 8. Webapp integration

**Integrated (`app.py`)**: `PressureLCDReader` runs as its own background
thread from Flask startup, independent of the main snapshot camera
(`config.CAMERA_INDEX`) — it opens `config.PRESSURE_OCR_CAM_INDEX` with its
own reconnect loop (retries every `PRESSURE_OCR_RECONNECT_DELAY` seconds if
the camera isn't there — the pump camera isn't guaranteed to always be
plugged in/dedicated). Accepted readings go straight into
`pressure_store[config.PRESSURE_OCR_PRESET]` via `_append_pressure_entry()`
— no HTTP call, since it's the same process. Status (connected, last
attempt, mode confirmed, last accepted value/time) is exposed at
`/api/pressure_ocr/status` and shown both on the main dashboard and at
`/embed/pressure_ocr` (for a Grafana panel, same pattern as
`/embed/temperature`).

Readings appear under a **separate preset** (`pressure_yolo` by default) in
the same pressure graph as manually-entered data, rather than replacing
manual entry — so the two can be compared while accuracy is still being
validated against a real pump.

**Standalone tool**: `--post` sends accepted readings to `/api/pressure`
over HTTP instead (under `--preset`, default also `pressure_yolo`) — useful
for testing against a *running* webapp without going through the integrated
reader. Without `--post`, it only writes local CSVs (dry-run).

## CLI reference

```
-i, --interval SEC       reading interval (default 10.0)
-c, --camera N           camera index (default 1 — second camera, distinct
                          from the webcam already used for /video_feed)
--conf FLOAT             YOLO detection confidence threshold (default 0.25)
--roi X Y W H            skip interactive ROI selection, use this region
--headless               no preview window
--ocr-conf-min FLOAT     min OCR confidence to accept (default 0.5)
--pressure-min FLOAT     lower sanity bound, mbar (default 1e-9)
--pressure-max FLOAT     upper sanity bound, mbar (default 1100 — above
                          atmospheric, covers pump-down from ambient)
--jump-decades FLOAT     max plausible jump vs last accepted, in decades
                          (default 3.0)
--max-retries N          extra attempts per cycle after a rejection
                          (default 2, i.e. 3 attempts total)
--retry-delay SEC        delay before a retry attempt (default 2.0)
--post                   actually POST accepted readings to the webapp
                          (default off — dry-run, local CSV only)
--api-url URL            webapp endpoint (default
                          http://localhost:8080/api/pressure)
--preset NAME             preset name for POSTed readings (default
                          pressure_yolo)
```

### `config.py` reference (the integrated reader)

```
PRESSURE_OCR_CAM_INDEX        camera index for the pump's LCD (default 1,
                              separate from CAMERA_INDEX used for snapshots)
PRESSURE_OCR_INTERVAL         seconds between reading attempts (default 20)
PRESSURE_OCR_ROI              (x, y, w, h) or None — see section 1
PRESSURE_OCR_PRESET           preset name accepted readings go under
                              (default "pressure_yolo")
PRESSURE_OCR_CONF_MIN         same as --ocr-conf-min (default 0.5)
PRESSURE_OCR_MIN / _MAX       same as --pressure-min/max (default 1e-9 / 1100)
PRESSURE_OCR_JUMP_DECADES     same as --jump-decades (default 3.0)
PRESSURE_OCR_MAX_RETRIES      same as --max-retries (default 2)
PRESSURE_OCR_RETRY_DELAY      same as --retry-delay (default 2.0)
PRESSURE_OCR_RECONNECT_DELAY  seconds between camera reconnect attempts when
                              disconnected (default 10)
PRESSURE_OCR_LOG_DIR          where the app's CSV logs go (default logs/)
```

## Known limitations / open items

- `E` misread as another letter (e.g. `F`) isn't parsed directly — relies on
  a retry landing on a clean read instead.
- Mode confirmation (`check_mode`) only knows about the pressure screen
  (code `340` + "Pressure" label). Speed/current screens aren't implemented
  yet — readings while the pump is showing those screens are correctly
  rejected as `mode_not_confirmed`, but not yet parsed into their own fields.
- All thresholds were tuned against the Tkinter simulator, not the physical
  pump — expect to revisit them (`config.PRESSURE_OCR_*`) once tested
  against real hardware.
- `PRESSURE_OCR_ROI` has to be calibrated once (via the standalone tool) and
  hardcoded into `config.py`. If the camera or pump display ever moves, the
  ROI needs re-calibrating — nothing detects a stale/wrong ROI automatically.
- CPU-only inference: one detect+OCR pass takes ~5–8s. Fine at the current
  10–30s intervals; would need a GPU or a lighter model to go faster. It
  also runs inside the Flask process (as a background thread) — heavy
  numpy/torch work during a reading attempt can briefly compete with Flask
  request handling for the GIL, though this hasn't been observed to cause
  noticeable issues in testing.

## Related files

- `pressure_ocr.py` (project root) — shared detect/OCR/validate core, see
  "Two entry points, one shared core" above.
- `YOLO_test/webcam_yolo_Rev1.1.py` — the earlier generic text-detector
  prototype this was built from (no pressure-specific parsing/validation).
- `YOLO_test/vacuum_pumpdown_simulator_V3.py` — the Tkinter LCD mockup used
  to test this before a physical pump is available.
- `app.py` → `PressureLCDReader`, `/api/pressure`,
  `/api/pressure_ocr/status`, `/embed/pressure_ocr`.
