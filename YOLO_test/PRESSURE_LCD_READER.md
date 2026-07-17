# pressure_lcd_reader.py

Reads the pressure value off a vacuum pump's LCD readout using a camera,
without needing any electrical/serial connection to the pump. Detects text
regions with a YOLO text-detector, reads them with EasyOCR, parses out the
`X.XE±N` pressure number, and only accepts it if it passes a few sanity
checks. Rejected readings trigger an immediate retry instead of waiting for
the next scheduled interval.

Branch: `yolo-extract-value`. Status: tested against
`vacuum_pumpdown_simulator_V3.py` (a Tkinter LCD mockup), not yet against a
physical pump. Not yet wired into `app.py` — runs standalone.

## Why this exists

The vacuum pressure panel in the webapp (`/api/pressure`) currently requires
someone to read the pump's LCD and type the value in by hand. This script
is meant to replace that manual step: point a webcam at the pump's display,
and have it auto-log (and eventually auto-post) the reading.

## Pipeline

```
camera frame → crop to ROI → YOLO detect text boxes → merge nearby boxes
  → EasyOCR per box → regex-parse pressure value → validate → accept/reject
  → write raw CSV (always) → write clean CSV + POST to webapp (if accepted)
```

### 1. ROI (region of interest)

On startup (unless `--roi` is given), a window pops up and you drag a box
around just the LCD readout, then press ENTER. Everything outside that box
is cropped away before detection ever runs.

This matters more than it sounds: in testing, running YOLO on the *full*
camera frame (e.g. a monitor showing the simulator window next to VSCode)
caused the box-merge step (see below) to merge unrelated on-screen text
across the whole width of the frame, occasionally swallowing the actual
pressure reading into a false merge. Cropping to just the LCD removes that
failure mode entirely, and also mirrors how the camera will actually be
mounted on a real pump (aimed tightly at the display, nothing else in frame).

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

Two CSVs per day, written to `YOLO_test/logs/`:

- **`pressure_raw_YYYY-MM-DD.csv`** — every attempt, accepted or not.
  Columns: `timestamp, attempt, raw_texts, parsed_value, ocr_conf, det_conf,
  code_seen, label_seen, decision, reason`. This is the debugging/tuning
  trail — if accuracy needs adjusting later, this file has everything
  needed to see why a reading was accepted or rejected.
- **`pressure_clean_YYYY-MM-DD.csv`** — only accepted readings.
  Columns: `timestamp, pressure_mbar`.

Example raw log row showing a retry recovering from a misread:

```
2026-07-17 14:14:15,1,"mbar | 340: | 2 .8F-4 | Pressure",,0.00,0.00,True,True,rejected,no_number_found
2026-07-17 14:14:18,2,"mbar | 340: | 2.9E-4 | Pressure",0.00029,0.99,0.89,True,True,accepted,ok
```

### 8. Webapp integration (not live by default)

`--post` sends accepted readings to the existing `/api/pressure` endpoint in
`app.py`, under preset `--preset` (default `pressure_yolo`, kept separate
from manually-entered presets so the two don't mix). Without `--post`, the
script only writes local CSVs (dry-run) — this is the current default while
accuracy is still being validated against a real pump.

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

## Known limitations / open items

- `E` misread as another letter (e.g. `F`) isn't parsed directly — relies on
  a retry landing on a clean read instead.
- Mode confirmation (`check_mode`) only knows about the pressure screen
  (code `340` + "Pressure" label). Speed/current screens aren't implemented
  yet — readings while the pump is showing those screens are correctly
  rejected as `mode_not_confirmed`, but not yet parsed into their own fields.
- All thresholds (`--ocr-conf-min`, `--pressure-min/max`, `--jump-decades`)
  were tuned against the Tkinter simulator, not the physical pump — expect
  to revisit them once tested against real hardware.
- Not yet integrated into `app.py` / the Flask process — this is a
  standalone script under `YOLO_test/`.
- CPU-only inference: one detect+OCR pass takes ~5–8s. Fine at the current
  10–30s intervals; would need a GPU or a lighter model to go faster.

## Related files

- `YOLO_test/webcam_yolo_Rev1.1.py` — the earlier generic text-detector
  prototype this was built from (no pressure-specific parsing/validation).
- `YOLO_test/vacuum_pumpdown_simulator_V3.py` — the Tkinter LCD mockup used
  to test this script before a physical pump is available.
- `app.py` → `/api/pressure` — the webapp endpoint this is meant to feed.
