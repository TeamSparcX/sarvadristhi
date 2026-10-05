"""Motion/object-event triage with OpenCV background subtraction.

Flags a compact moving object (not texture or global change) and reports when it appears.
Output is TRIAGE with a confidence score, never a finding of fact."""
import subprocess

import cv2
import numpy as np

W = 320


def _frames(mp4: str, fps: float):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", mp4], capture_output=True, text=True)
    w, h = map(int, r.stdout.strip().split(",")[:2])
    hh = int(round(h * W / w / 2) * 2)
    p = subprocess.Popen(["ffmpeg", "-v", "quiet", "-i", mp4, "-vf", f"fps={fps},scale={W}:{hh}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
    n = W * hh
    k = 0
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield k / fps, np.frombuffer(b, np.uint8).reshape(hh, W)
        k += 1
    p.wait()


def detect_events(mp4: str, sample_fps: float = 4.0, min_area=0.004, max_area=0.25,
                  min_solidity=0.6, min_len_s=0.75, merge_s=1.5, exclude=(), src_width=None) -> list[dict]:
    """exclude: (x0, y0, x1, y1) boxes in source pixels, e.g. the OCR-located overlay text
    (a running clock is always 'moving')."""
    mog = cv2.createBackgroundSubtractorMOG2(history=int(sample_fps * 20), varThreshold=25,
                                             detectShadows=False)
    hits = []
    kernel = np.ones((5, 5), np.uint8)
    mask = None
    for t, g in _frames(mp4, sample_fps):
        if mask is None:
            mask = np.full(g.shape, 255, np.uint8)
            k = g.shape[1] / (src_width or g.shape[1])
            for x0, y0, x1, y1 in exclude:
                cv2.rectangle(mask, (int(x0 * k) - 2, int(y0 * k) - 2), (int(x1 * k) + 2, int(y1 * k) + 2), 0, -1)
        fg = cv2.bitwise_and(mog.apply(g), mask)
        if t < 2:            # learning period
            continue
        if (fg > 0).mean() > 0.3:   # global change (lighting, IR switch, shake): not an object
            continue
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
        n, lab, st, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
        best = None
        for i in range(1, n):
            x, y, w, h, a = st[i]
            frac = a / g.size
            if min_area <= frac <= max_area and a / (w * h) >= min_solidity:
                if best is None or a > best[4]:
                    best = (x, y, w, h, a)
        if best:
            hits.append((t, best[4] / g.size, best[:4]))
    events, cur = [], None
    for t, frac, box in hits:
        if cur and t - cur["end_s"] <= merge_s:
            cur["end_s"], cur["peak_area"] = t, max(cur["peak_area"], frac)
            cur["n"] += 1
        else:
            if cur:
                events.append(cur)
            cur = {"start_s": t, "end_s": t, "peak_area": frac, "n": 1, "box": [int(v) for v in box]}
    if cur:
        events.append(cur)
    out = []
    for e in events:
        if e["end_s"] - e["start_s"] >= min_len_s:
            conf = float(min(0.95, 0.4 + 0.1 * e["n"] / sample_fps + 2 * e["peak_area"]))
            out.append({"type": "moving-object", "start_s": round(e["start_s"], 2),
                        "end_s": round(e["end_s"], 2), "confidence": round(conf, 2),
                        "basis": "background subtraction, compact foreground blob"})
    return out
