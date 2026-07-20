"""Calibrate the LCD character grid and cut character templates from it.

This is the one-time (well, occasionally-repeated) setup step behind
lcd_template_ocr.py. Two things come out of it, both into lcd_templates/:

  grid.json  - where the character grid sits inside the ROI
  <char>.png - a bitmap per character, cut from the real display

Run it against a saved snapshot (easiest — no need to stand at the pump) or
against the live camera:

    python YOLO_test/build_lcd_templates.py --image snapshots/snapshot_....jpg
    python YOLO_test/build_lcd_templates.py --camera 1

Workflow:
  1. Line the red grid up with the characters using the trackbars. Each cell
     should hold exactly one character, reasonably centred.
  2. Press 't' and type what each row actually reads, e.g. "340: Pressure".
     Every non-space cell gets saved as a template for that character.
  3. Press 's' to save the grid, 'q' when done.

Templates accumulate across runs, so you don't need one image showing all ten
digits — run it again on a later snapshot showing a different pressure and it
fills in whatever's missing. It prints which characters are still unseen. The
digits that matter most are the ones a pressure reading can contain: 0-9, '.',
'E', '-', '+', plus ':' for the "340:" mode check.

Keys:
  t  type the text for each row and cut templates from it
  s  save grid.json
  d  dump the sliced cells to lcd_templates/_debug_cells.png for inspection
  r  reload/reset the grid to defaults
  q  quit
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lcd_template_ocr as lt  # noqa: E402

WINDOW = 'LCD grid calibration'
# Pressure readings only ever contain these; ':' is for the "340:" mode check.
WANTED = list('0123456789.E-+:')


def load_roi(explicit):
    if explicit:
        return tuple(explicit)
    roi_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'pressure_ocr_roi.json')
    if os.path.isfile(roi_file):
        with open(roi_file, encoding='utf-8') as f:
            roi = json.load(f).get('roi')
            if roi:
                print(f'Using ROI from pressure_ocr_roi.json: {tuple(roi)}')
                return tuple(roi)
    return None


def grid_from_trackbars():
    g = dict(lt.DEFAULT_GRID)
    g.update({
        'x0': cv2.getTrackbarPos('x0', WINDOW),
        'y0': cv2.getTrackbarPos('y0', WINDOW),
        'pitch_x': max(4, cv2.getTrackbarPos('pitch_x', WINDOW)),
        'pitch_y': max(4, cv2.getTrackbarPos('pitch_y', WINDOW)),
        'cell_w': max(3, cv2.getTrackbarPos('cell_w', WINDOW)),
        'cell_h': max(3, cv2.getTrackbarPos('cell_h', WINDOW)),
        'n_cols': max(1, cv2.getTrackbarPos('n_cols', WINDOW)),
        'n_rows': max(1, cv2.getTrackbarPos('n_rows', WINDOW)),
        'block_size': max(3, cv2.getTrackbarPos('block', WINDOW)) | 1,
        'c': cv2.getTrackbarPos('C', WINDOW) - 30,
    })
    return g


def draw_grid(binary, grid):
    vis = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    for r in range(grid['n_rows']):
        for c in range(grid['n_cols']):
            x, y, w, h = lt.cell_rect(grid, r, c)
            colour = (0, 0, 255) if r == 0 else (0, 200, 255)
            cv2.rectangle(vis, (x, y), (x + w, y + h), colour, 1)
    return vis


def save_templates(binary, grid, out_dir):
    """Ask what each row says, then cut a template per non-space cell.

    Returns the text given for row 0, which becomes the alignment anchor —
    that row is the same on every frame, so matching against it is how the
    reader finds the grid again after the camera gets nudged.
    """
    print('\nType what each row reads (leave blank to skip a row).')
    print('Use exactly the characters shown, including spaces, e.g. "340: Pressure"')
    print('Leading spaces matter — the value line is right-aligned on this display.')
    saved, skipped = 0, 0
    row0_text = None
    for r in range(grid['n_rows']):
        try:
            text = input(f'  row {r}: ')
        except EOFError:
            return row0_text
        if not text.strip():
            continue
        if r == 0:
            row0_text = text
        for c, ch in enumerate(text):
            if c >= grid['n_cols']:
                print(f'    (row {r} text is longer than n_cols={grid["n_cols"]}, truncated)')
                break
            if ch == ' ':
                continue
            cell = lt.extract_cell(binary, grid, r, c)
            if cell is None:
                continue
            if lt.ink_ratio(cell) < lt.BLANK_INK_RATIO:
                print(f'    cell (row {r}, col {c}) for {ch!r} looks blank — grid misaligned? skipped')
                skipped += 1
                continue
            name = lt.char_to_name(ch)
            idx = 0
            while os.path.exists(os.path.join(out_dir, f'{name}_{idx}.png')):
                idx += 1
            cv2.imwrite(os.path.join(out_dir, f'{name}_{idx}.png'), cell)
            saved += 1
    print(f'Saved {saved} template(s)' + (f', skipped {skipped} blank cell(s)' if skipped else ''))
    report_missing(out_dir)
    return row0_text


def report_missing(out_dir):
    have = set()
    for fn in os.listdir(out_dir):
        if fn.endswith('.png') and not fn.startswith('_'):
            have.add(lt.name_to_char(fn[:-4].rsplit('_', 1)[0]))
    missing = [ch for ch in WANTED if ch not in have]
    print(f'Characters collected: {"".join(sorted(have)) or "(none)"}')
    if missing:
        print(f'Still missing: {"".join(missing)}'
              '  <- run again on a snapshot showing these (a different pressure value)')
    else:
        print('All characters needed for a pressure reading are collected.')


def dump_cells(binary, grid, out_dir):
    """Save a montage of the sliced cells so misalignment is obvious."""
    rows = []
    for r in range(grid['n_rows']):
        cells = []
        for c in range(grid['n_cols']):
            cell = lt.extract_cell(binary, grid, r, c)
            if cell is None:
                cell = np.zeros((grid['cell_h'], grid['cell_w']), np.uint8)
            cells.append(cv2.copyMakeBorder(cell, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=128))
        rows.append(np.hstack(cells))
    width = max(r.shape[1] for r in rows)
    rows = [cv2.copyMakeBorder(r, 0, 0, 0, width - r.shape[1], cv2.BORDER_CONSTANT, value=64)
            for r in rows]
    path = os.path.join(out_dir, '_debug_cells.png')
    cv2.imwrite(path, np.vstack(rows))
    print(f'Wrote {path}')


def main():
    ap = argparse.ArgumentParser(description='Calibrate the LCD grid and cut character templates')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--image', help='Calibrate from a saved snapshot')
    src.add_argument('--camera', type=int, help='Calibrate from a live camera index')
    ap.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'),
                    help='ROI within the frame (default: read pressure_ocr_roi.json)')
    args = ap.parse_args()

    os.makedirs(lt.TEMPLATE_DIR, exist_ok=True)

    cap = None
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f'Cannot read image: {args.image}')
            return
    else:
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print(f'Cannot open camera index {args.camera}')
            return
        ok, frame = cap.read()
        if not ok:
            print('Cannot grab a frame')
            return

    roi = load_roi(args.roi)
    if roi is None:
        print('No ROI given and no pressure_ocr_roi.json — select the LCD area now.')
        r = cv2.selectROI('Select the LCD screen area', frame, showCrosshair=True)
        cv2.destroyWindow('Select the LCD screen area')
        roi = tuple(int(v) for v in r) if r[2] and r[3] else (0, 0, frame.shape[1], frame.shape[0])
    print(f'ROI = {roi}')

    # Start from a saved grid if there is one, else the defaults.
    start = dict(lt.DEFAULT_GRID)
    grid_path = os.path.join(lt.TEMPLATE_DIR, lt.GRID_FILE)
    if os.path.isfile(grid_path):
        with open(grid_path, encoding='utf-8') as f:
            start.update(json.load(f))
        print('Loaded existing grid.json as the starting point')

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.createTrackbar('x0', WINDOW, start['x0'], 400, lambda v: None)
    cv2.createTrackbar('y0', WINDOW, start['y0'], 400, lambda v: None)
    cv2.createTrackbar('pitch_x', WINDOW, start['pitch_x'], 120, lambda v: None)
    cv2.createTrackbar('pitch_y', WINDOW, start['pitch_y'], 200, lambda v: None)
    cv2.createTrackbar('cell_w', WINDOW, start['cell_w'], 120, lambda v: None)
    cv2.createTrackbar('cell_h', WINDOW, start['cell_h'], 120, lambda v: None)
    cv2.createTrackbar('n_cols', WINDOW, start['n_cols'], 24, lambda v: None)
    cv2.createTrackbar('n_rows', WINDOW, start['n_rows'], 4, lambda v: None)
    cv2.createTrackbar('block', WINDOW, start['block_size'], 151, lambda v: None)
    cv2.createTrackbar('C', WINDOW, start['c'] + 30, 60, lambda v: None)

    anchor_text = start.get('anchor_text', lt.DEFAULT_GRID['anchor_text'])

    print('\nLine the grid up so each cell holds one character, then:')
    print("  't' = type the rows and cut templates,  's' = save grid,"
          "  'd' = dump cells,  'q' = quit\n")
    report_missing(lt.TEMPLATE_DIR)

    x, y, w, h = roi
    while True:
        if cap is not None:
            ok, live = cap.read()
            if ok:
                frame = live
        crop = frame[y:y + h, x:x + w]
        grid = grid_from_trackbars()
        binary = lt.preprocess(crop, grid)
        vis = draw_grid(binary, grid)
        cv2.putText(vis, "t=type rows  s=save grid  d=dump cells  q=quit",
                    (8, vis.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
        cv2.imshow(WINDOW, vis)

        key = cv2.waitKey(30) & 0xFF
        if key in (ord('q'), 27):
            break
        elif key == ord('t'):
            typed = save_templates(binary, grid, lt.TEMPLATE_DIR)
            if typed:
                anchor_text = typed
                print(f'Alignment anchor set to {anchor_text!r} (press s to save)')
        elif key == ord('d'):
            dump_cells(binary, grid, lt.TEMPLATE_DIR)
        elif key == ord('s'):
            grid['roi'] = list(roi)
            grid['anchor_row'] = 0
            grid['anchor_text'] = anchor_text
            with open(grid_path, 'w', encoding='utf-8') as f:
                json.dump(grid, f, indent=2)
            print(f'Saved {grid_path}  (anchor: {anchor_text!r})')
        elif key == ord('r'):
            for k, v in lt.DEFAULT_GRID.items():
                if k in ('x0', 'y0', 'pitch_x', 'pitch_y', 'cell_w', 'cell_h', 'n_cols', 'n_rows'):
                    cv2.setTrackbarPos(k, WINDOW, v)
            print('Reset grid trackbars to defaults')

    if cap is not None:
        cap.release()
    cv2.destroyAllWindows()


if __name__ == '__main__':
    main()
