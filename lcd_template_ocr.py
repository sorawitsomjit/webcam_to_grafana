"""Read the pump's fixed-pitch dot-matrix LCD by template matching.

Why not general OCR: the Pfeiffer DCU002 draws a dot-matrix font that
EasyOCR/Tesseract were never trained on, and it slashes its zeros. Real logs
showed "0" coming back as l/i/]/Z/I/O and "3" as S, for an overall accept rate
of only a few percent even after ROI, contrast and exposure tuning.

The display is a character LCD, so every glyph sits on a regular grid — the
character pitch measured straight off a snapshot is constant across the whole
line. That means we can slice cells off the grid and compare each one against
templates cut from this exact display, which sidesteps the unfamiliar-font
problem entirely instead of fighting it.

Two pieces are needed before this can run, both produced by
YOLO_test/build_lcd_templates.py:
  lcd_templates/grid.json  - where the character grid sits within the ROI
  lcd_templates/*.png      - one or more bitmaps per character

Preprocessing here must stay identical to the calibration tool's, or the
templates won't line up with what we slice at runtime.
"""
import json
import os

import cv2
import numpy as np

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lcd_templates')
GRID_FILE = 'grid.json'

# Characters whose literal form can't go in a filename.
_NAME_TO_CHAR = {'dot': '.', 'colon': ':', 'minus': '-', 'plus': '+', 'space': ' '}
_CHAR_TO_NAME = {v: k for k, v in _NAME_TO_CHAR.items()}

# A cell with almost no ink is a space, not a badly-matched glyph.
BLANK_INK_RATIO = 0.02

DEFAULT_GRID = {
    'upscale': 3,
    'x0': 68, 'y0': 70,
    'pitch_x': 46, 'pitch_y': 73,
    'cell_w': 40, 'cell_h': 44,
    'n_cols': 16, 'n_rows': 2,
    'block_size': 61, 'c': 15,
    'align_range': 6,
    # Text that never changes on this screen, used as an alignment fiducial.
    'anchor_row': 0,
    'anchor_text': '340: Pressure',
}


def char_to_name(ch):
    return _CHAR_TO_NAME.get(ch, ch)


def name_to_char(name):
    return _NAME_TO_CHAR.get(name, name)


def preprocess(crop, grid):
    """BGR ROI crop -> upscaled binary image (ink = white).

    Adaptive (not global) threshold because the LCD picks up uneven glare
    across its face; a single global cutoff loses the dim half of the screen.
    """
    up = grid['upscale']
    if up != 1:
        crop = cv2.resize(crop, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    block = grid['block_size'] | 1        # must be odd
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, block, grid['c'])


def cell_rect(grid, row, col, dx=0, dy=0):
    x = grid['x0'] + col * grid['pitch_x'] + dx
    y = grid['y0'] + row * grid['pitch_y'] + dy
    return x, y, grid['cell_w'], grid['cell_h']


def extract_cell(binary, grid, row, col, dx=0, dy=0):
    x, y, w, h = cell_rect(grid, row, col, dx, dy)
    if x < 0 or y < 0 or x + w > binary.shape[1] or y + h > binary.shape[0]:
        return None
    return binary[y:y + h, x:x + w]


def ink_ratio(cell):
    return float((cell > 0).sum()) / cell.size if cell is not None and cell.size else 0.0


class LCDTemplateOCR:
    def __init__(self, template_dir=TEMPLATE_DIR):
        self.dir = template_dir
        self.grid = None
        self.templates = {}     # char -> [binary bitmap, ...]
        self.load()

    # ── loading ──────────────────────────────────────────────────────────

    def load(self):
        self.grid = None
        self.templates = {}
        grid_path = os.path.join(self.dir, GRID_FILE)
        if not os.path.isfile(grid_path):
            return
        with open(grid_path, encoding='utf-8') as f:
            self.grid = {**DEFAULT_GRID, **json.load(f)}
        if not os.path.isdir(self.dir):
            return
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith('.png'):
                continue
            name = fn[:-4].rsplit('_', 1)[0]
            img = cv2.imread(os.path.join(self.dir, fn), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            self.templates.setdefault(name_to_char(name), []).append(img)

    def available(self):
        return self.grid is not None and bool(self.templates)

    def known_chars(self):
        return sorted(self.templates.keys())

    # ── matching ─────────────────────────────────────────────────────────

    def _match_cell(self, cell):
        """-> (char, score). Blank cells short-circuit to a space."""
        if ink_ratio(cell) < BLANK_INK_RATIO:
            return ' ', 1.0
        best_char, best_score = '?', -1.0
        for ch, variants in self.templates.items():
            for tmpl in variants:
                t = tmpl
                if t.shape != cell.shape:
                    t = cv2.resize(t, (cell.shape[1], cell.shape[0]))
                score = float(cv2.matchTemplate(cell, t, cv2.TM_CCOEFF_NORMED)[0][0])
                if score > best_score:
                    best_char, best_score = ch, score
        return best_char, best_score

    def _score_text_at(self, binary, row, text, dx, dy):
        """Mean match score for `text` laid along `row` at the given offset."""
        total, n = 0.0, 0
        for c, ch in enumerate(text):
            if ch == ' ' or c >= self.grid['n_cols']:
                continue
            variants = self.templates.get(ch)
            if not variants:
                continue
            cell = extract_cell(binary, self.grid, row, c, dx, dy)
            if cell is None:
                return None
            best = -1.0
            for tmpl in variants:
                t = tmpl if tmpl.shape == cell.shape else cv2.resize(tmpl, (cell.shape[1], cell.shape[0]))
                best = max(best, float(cv2.matchTemplate(cell, t, cv2.TM_CCOEFF_NORMED)[0][0]))
            total += best
            n += 1
        return total / n if n else None

    def _align(self, binary):
        """Find the (dx, dy) that best re-centres the grid on the text.

        The camera sits on a stand rather than being bolted down, so the ROI
        drifts by a few pixels between sessions. Rather than demand a
        re-calibration for every nudge, hunt for the offset that fits best and
        shift to suit.

        The label row ("340: Pressure") is the same on every frame, which makes
        it a natural fiducial: score candidate offsets by how well that known
        text matches. Falls back to "catch the most ink" when no anchor text
        was recorded, which is weaker — on a dim frame it can lock onto the
        wrong column entirely.
        """
        rng = self.grid.get('align_range', 6)
        if rng <= 0:
            return 0, 0
        g = self.grid
        anchor = g.get('anchor_text')
        anchor_row = g.get('anchor_row', 0)

        best, best_score = (0, 0), -1e9
        for dy in range(-rng, rng + 1):
            for dx in range(-rng, rng + 1):
                if anchor:
                    score = self._score_text_at(binary, anchor_row, anchor, dx, dy)
                    if score is None:
                        continue
                else:
                    score = 0.0
                    ok = True
                    for r in range(g['n_rows']):
                        for c in range(g['n_cols']):
                            cell = extract_cell(binary, g, r, c, dx, dy)
                            if cell is None:
                                ok = False
                                break
                            score += (cell > 0).sum()
                        if not ok:
                            break
                    if not ok:
                        continue
                if score > best_score:
                    best, best_score = (dx, dy), score
        return best

    def read(self, crop):
        """Read the LCD out of a BGR ROI crop.

        Returns {'lines', 'text', 'scores', 'mean_score', 'binary', 'offset'}.
        Parsing a pressure out of the text is the caller's job.
        """
        if not self.available():
            return None
        binary = preprocess(crop, self.grid)
        dx, dy = self._align(binary)

        lines, scores = [], []
        for r in range(self.grid['n_rows']):
            chars, row_scores = [], []
            for c in range(self.grid['n_cols']):
                cell = extract_cell(binary, self.grid, r, c, dx, dy)
                if cell is None:
                    continue
                ch, sc = self._match_cell(cell)
                chars.append(ch)
                if ch != ' ':
                    row_scores.append(sc)
            lines.append(''.join(chars).rstrip())
            scores.append(row_scores)

        flat = [s for row in scores for s in row]
        return {
            'lines': lines,
            'text': ' | '.join(ln for ln in lines if ln.strip()),
            'scores': scores,
            'mean_score': float(np.mean(flat)) if flat else 0.0,
            'min_score': float(np.min(flat)) if flat else 0.0,
            'offset': (dx, dy),
            'binary': binary,
        }
