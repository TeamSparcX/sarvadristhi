"""Overlay OCR: decode keyframes, read the burned-in camera label and clock.

Two stages, because full-frame OCR on busy footage is slow and noisy:
  1. locate: OCR the top/bottom bands of a small sample of frames and record where the
     clock and the camera label sit (DVR overlays are fixed-position);
  2. read: OCR only those small regions for every frame, many crops per Tesseract call.
Backend is Tesseract (offline). A PaddleOCR backend can replace _ocr_sheet."""
import os
import re
import shutil
import subprocess
import tempfile
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

BAND = 0.18
SHEET = 60
PAD = 6
FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1",
                     "i": "1", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9"})
_T = r"\D{0,3}(\d{2})[:.\s]?(\d{2})[:.\s]?(\d{2})"     # separators are often lost in OCR
DT_PATTERNS = [
    (re.compile(r"(\d{4})[-/.\s](\d{2})[-/.\s](\d{2})" + _T), "ymd"),
    (re.compile(r"(\d{2})[-/.\s](\d{2})[-/.\s](\d{4})" + _T), "dmy"),
]
CHAN_RE = re.compile(r"(?:CAMERA|CAM|CH)\s*[-_ ]?\s*([0-9OQDo]*[0-9][0-9OQDo]*)(?![0-9])", re.I)
DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "Q": "0", "q": "0", "D": "0", "d": "0"})
DATEISH = re.compile(r"\d[\d:/.\-]{3,}")


def available() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("ffmpeg"))


# ------------------------------------------------------------------ decoding
def _fmt(path: Path) -> str:
    from .formats.codec import ffmpeg_format, sniff
    with open(path, "rb") as f:
        return ffmpeg_format(sniff(f.read(1 << 20)))


def _probe_size(path: Path) -> tuple[int, int] | None:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-f", _fmt(path), "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        w, h = map(int, r.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        return None


def _decode_one(path: Path):
    r = subprocess.run(["ffmpeg", "-v", "quiet", "-f", _fmt(path), "-i", str(path), "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR) if r.stdout else None


def decode_keyframes(es_list: list[bytes]) -> list[np.ndarray | None]:
    """Decode IDR access units (each carrying SPS/PPS). One FFmpeg call for the batch;
    falls back to one call per unit if any unit fails or resolutions differ."""
    if not es_list:
        return []
    with tempfile.TemporaryDirectory() as d:
        cat = Path(d) / "all.h264"
        cat.write_bytes(b"".join(es_list))
        size = _probe_size(cat)
        if size:
            w, h = size
            r = subprocess.run(["ffmpeg", "-v", "quiet", "-f", _fmt(cat), "-i", str(cat), "-fps_mode",
                                "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
            n = len(r.stdout) // (w * h * 3)
            if n == len(es_list) and len(r.stdout) == n * w * h * 3:
                arr = np.frombuffer(r.stdout, np.uint8).reshape(n, h, w, 3)
                return [arr[k].copy() for k in range(n)]
        out = []
        for k, es in enumerate(es_list):
            p = Path(d) / f"k{k}.h264"
            p.write_bytes(es)
            out.append(_decode_one(p))
        return out


# ------------------------------------------------------------------ tesseract
def _prep(crop: np.ndarray, scale: int = 2, overlay: bool = False) -> np.ndarray:
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if overlay:
        box = _overlay_box(g)
        if box:                        # text on a dark backing box: crop to it, then Otsu
            x0, y0, x1, y1 = box
            g = g[y0:y1, x0:x1]
            _, fgm = cv2.threshold(g, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:                          # outlined text: keep near-white glyph pixels
            fgm = (g >= 170).astype(np.uint8)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(fgm.astype(np.uint8), connectivity=8)
        keep = np.zeros(n, bool)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= 2 * scale * scale   # drop single-pixel specks
        bw = np.where(keep[lab], 0, 255).astype(np.uint8)
        return cv2.copyMakeBorder(bw, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if bw.mean() < 128:
        bw = 255 - bw
    return bw


def _overlay_box(g: np.ndarray):
    """Bounding box of a dark overlay backing box inside the crop, if there is one."""
    dark = cv2.morphologyEx((g < 70).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(dark, connectivity=4)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(v) for v in stats[k, :4])
    if w * h >= 0.25 * g.size and h >= 0.35 * g.shape[0] and stats[k, cv2.CC_STAT_AREA] >= 0.6 * w * h:
        return x, y, x + w, y + h
    return None


def _tsv(img: np.ndarray, psm: int, extra=()) -> list[list[str]]:
    # Windows: Tesseract cannot open a file that is still held open by Python.
    name = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            name = f.name
        cv2.imwrite(name, img)
        r = subprocess.run(["tesseract", name, "stdout", "--psm", str(psm), *extra, "tsv"],
                           capture_output=True, text=True, encoding="utf-8",  # Windows: Tesseract emits UTF-8, not cp1252
                           env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    finally:
        if name:
            Path(name).unlink(missing_ok=True)
    rows = []
    for line in r.stdout.splitlines()[1:]:
        p = line.split("\t")
        if len(p) >= 12 and p[11].strip() and float(p[10]) >= 0:
            rows.append(p)
    return rows


def _ocr_sheet(crops: list[np.ndarray], psm: int = 6, whitelist: str | None = None) -> list[str]:
    """OCR many crops stacked into one image; returns text per crop."""
    w = max(c.shape[1] for c in crops)
    gap, y, parts, spans = 20, 0, [], []
    for c in crops:
        pad = np.full((c.shape[0], w), 255, np.uint8)
        pad[:, :c.shape[1]] = c
        parts += [pad, np.full((gap, w), 255, np.uint8)]
        spans.append((y, y + c.shape[0]))
        y += c.shape[0] + gap
    words = [[] for _ in crops]
    extra = ("-c", f"tessedit_char_whitelist={whitelist}") if whitelist else ()
    for p in _tsv(np.vstack(parts), psm, extra):
        cy = int(p[7]) + int(p[9]) / 2
        for k, (a, b) in enumerate(spans):
            if a <= cy < b:
                words[k].append(((int(p[2]), int(p[3]), int(p[4])), int(p[6]), p[11]))
                break
    return [" ".join(t for _, _, t in sorted(ws)) for ws in words]


def ocr_crops(crops: list[np.ndarray], whitelist: str | None = None) -> list[str]:
    prepped = [_prep(c, overlay=True) for c in crops]
    chunks = [prepped[i:i + SHEET] for i in range(0, len(prepped), SHEET)]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        res = list(ex.map(lambda c: _ocr_sheet(c, 6, whitelist), chunks))
    return [t for r in res for t in r]


# ------------------------------------------------------------------ parsing
def parse_datetime(text: str) -> int | None:
    cand = text.translate(FIX)
    for rx, order in DT_PATTERNS:
        for m in rx.finditer(cand):
            a, b, c, hh, mm, ss = map(int, m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            if 2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mm < 60 and ss < 60:
                return timegm((y, mo, d, hh, mm, ss, 0, 0, 0))
    return None


def parse_label(text: str) -> tuple[str | None, int | None]:
    t = re.sub(r"[^A-Za-z0-9 _\-]", "", text)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.search(r"[A-Za-z]{2}", t):
        return None, None
    m = CHAN_RE.search(t)
    if m:
        t = t[m.start():]           # drop OCR junk before the camera token
        digits = m.group(1).translate(DIGIT_FIX)
        return t, (int(digits) if digits.isdigit() else None)
    return t, None


# ------------------------------------------------------------------ locating overlays
def _union(boxes):
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    return x0, y0, x1, y1


def locate_rois(images: list, sample: int = 12) -> dict:
    """Find where the clock and the label sit, from a sample of frames."""
    imgs = [im for im in images if im is not None]
    if not imgs:
        return {}
    pick = [imgs[int(k * (len(imgs) - 1) / max(1, sample - 1))] for k in range(min(sample, len(imgs)))]
    clock, label = [], []
    for im in pick:
        h, w = im.shape[:2]
        bh = max(16, int(h * BAND))
        for y0 in (0, h - bh):
            rows = _tsv(_prep(im[y0:y0 + bh]), 11)
            joined = " ".join(p[11] for p in rows)
            if not (parse_datetime(joined) or CHAN_RE.search(joined)):   # try glyph-mode binarisation
                g = cv2.cvtColor(im[y0:y0 + bh], cv2.COLOR_BGR2GRAY)
                g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                rows = _tsv(np.where(g >= 200, 0, 255).astype(np.uint8), 11)
            lines = {}
            for p in rows:
                box = (int(p[6]) // 2, y0 + int(p[7]) // 2, (int(p[6]) + int(p[8])) // 2,
                       y0 + (int(p[7]) + int(p[9])) // 2)
                lines.setdefault((p[2], p[3], p[4]), []).append((box, p[11]))
            for words in lines.values():
                text = " ".join(t for _, t in words)
                if parse_datetime(text):
                    clock.append(_union([b for b, t in words if DATEISH.search(t.translate(FIX))]))
                m = [k for k, (_, t) in enumerate(words) if CHAN_RE.search(t)]
                if m:
                    k = m[0]
                    run = [words[k][0]]
                    for b, t in words[k + 1:]:
                        if re.fullmatch(r"[A-Za-z0-9_\-]+", t) and b[0] - run[-1][2] < 3 * (b[3] - b[1]):
                            run.append(b)
                        else:
                            break
                    label.append(_union(run))

    def settle(boxes, w, h):
        if not boxes:
            return None
        x0, y0, x1, y1 = _union(boxes)
        return max(0, x0 - PAD), max(0, y0 - PAD), min(w, x1 + PAD), min(h, y1 + PAD)

    h, w = pick[0].shape[:2]
    lab = settle(label, w, h)
    if lab:
        lab = (lab[0], lab[1], min(w, lab[2] + max(48, (lab[2] - lab[0]) // 2)), lab[3])
    return {"clock": settle(clock, w, h), "label": lab, "sampled": len(pick)}


def read_overlays(images: list, rois: dict | None = None) -> tuple[list[dict], dict]:
    """Return per-image {'label','channel','clock_s'} and the regions used."""
    rois = rois or locate_rois(images)
    res = [{"label": None, "channel": None, "clock_s": None} for _ in images]
    for kind in ("clock", "label"):
        box = rois.get(kind)
        if not box:
            continue
        x0, y0, x1, y1 = box
        idx = [k for k, im in enumerate(images) if im is not None]
        wl = "0123456789-:/. " if kind == "clock" else None
        texts = ocr_crops([images[k][y0:y1, x0:x1] for k in idx], wl)
        for k, t in zip(idx, texts):
            if kind == "clock":
                res[k]["clock_s"] = parse_datetime(t)
            else:
                res[k]["label"], res[k]["channel"] = parse_label(t)
    return res, rois


def _word_key(w: str) -> str:
    return w.upper()[:2]


def consensus_label(readings, min_share: float = 0.2) -> str | None:
    """Camera name from many noisy readings.
    Camera token: most common exact spelling among readings carrying the most common number.
    Name words: the first words after the token, grouped when they share their first two
    letters and are similar (OCR misspellings of one word). The strongest group is kept if it
    appears in >= min_share of readings; other groups are kept if at least half as strong."""
    import difflib
    from collections import Counter
    readings = [r for r in readings if r]
    named = []
    for r in readings:
        m = CHAN_RE.search(r)
        if m:
            d = m.group(1).translate(DIGIT_FIX)
            if d.isdigit() and len(d) <= 3:
                named.append((int(d), r[m.start():m.end()], r[m.end():].split()[:3]))
    if not named:
        return parse_label(Counter(readings).most_common(1)[0][0])[0] if readings else None
    num = Counter(n for n, _, _ in named).most_common(1)[0][0]
    named = [x for x in named if x[0] == num]
    token = Counter(t for _, t, _ in named).most_common(1)[0][0]
    groups = []          # dicts: forms Counter, positions, n readings containing it
    for _, _, words in named:
        seen = set()
        for pos, w in enumerate(words):
            if len(w) < 3 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9\-]*", w):
                continue
            g = next((g for g in groups if _word_key(w) == g["key"] and difflib.SequenceMatcher(
                None, w.upper(), g["forms"].most_common(1)[0][0].upper()).ratio() >= 0.6), None)
            if g is None:
                g = {"key": _word_key(w), "forms": Counter(), "pos": [], "n": 0}
                groups.append(g)
            g["forms"][w] += 1
            g["pos"].append(pos)
            if id(g) not in seen:
                g["n"] += 1
                seen.add(id(g))
    if not groups:
        return token
    top = max(g["n"] for g in groups)
    if top < max(2, min_share * len(named)):
        return token
    keep = sorted((g for g in groups if g["n"] >= 0.5 * top), key=lambda g: sum(g["pos"]) / len(g["pos"]))
    return " ".join([token] + [g["forms"].most_common(1)[0][0] for g in keep])
