"""Camera attribution by sensor-noise fingerprint (PRNU-style) for footage with no overlay.

Each keyframe's noise residual (frame minus a denoised copy) carries the camera sensor's
fixed pattern. Edges and saturated pixels are masked, rows/columns zero-meaned. Keyframes
are linked when their residuals correlate clearly above the cross-camera background level;
linked groups become fingerprints, and every keyframe is then assigned to the fingerprint it
matches best. Strong fixed-view scene content also correlates, so results are reported as
attribution clusters with their correlation statistics, never as a sensor identification."""
import cv2
import numpy as np

CROP = 256


def residual(gray: np.ndarray) -> np.ndarray:
    f = gray.astype(np.float32)
    d = cv2.GaussianBlur(cv2.medianBlur(gray.astype(np.uint8), 3).astype(np.float32), (0, 0), 1.0)
    w = f - d
    g = np.hypot(cv2.Sobel(d, cv2.CV_32F, 1, 0), cv2.Sobel(d, cv2.CV_32F, 0, 1))
    w *= (g < 20) & (f > 20) & (f < 235)
    w -= w.mean(axis=1, keepdims=True)
    w -= w.mean(axis=0, keepdims=True)
    return w


def _vec(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    c = min(CROP, h, w)
    y0, x0 = (h - c) // 2, (w - c) // 2
    v = residual(gray)[y0:y0 + c, x0:x0 + c].ravel()
    v = v - v.mean()
    n = np.linalg.norm(v)
    return v / n if n else v


def _otsu(v: np.ndarray) -> float:
    hist, edges = np.histogram(v, bins=256)
    p = hist / hist.sum()
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(p)
    m0 = np.cumsum(p * mids)
    mt = m0[-1]
    between = (mt * w0 - m0) ** 2 / np.maximum(w0 * (1 - w0), 1e-12)
    return float(mids[int(np.argmax(between))])


def cluster(images: list, min_size: int = 3, separation: float = 5.0) -> dict:
    """images: BGR or grey frames (None allowed). Returns {'labels': [...], 'clusters': n, ...}."""
    idx = [k for k, im in enumerate(images) if im is not None]
    if len(idx) < 2 * min_size:
        return {"labels": [None] * len(images), "clusters": 0, "reason": "too few keyframes"}
    X = np.stack([_vec(cv2.cvtColor(images[k], cv2.COLOR_BGR2GRAY) if images[k].ndim == 3 else images[k])
                  for k in idx]).astype(np.float32)
    C = X @ X.T
    off = C[np.triu_indices(len(idx), 1)]
    med = float(np.median(off))
    thr = _otsu(off)
    lo, hi = off[off < thr], off[off >= thr]
    # accept only a clearly bimodal split: matching pairs far above the non-matching level
    if len(lo) < 2 or len(hi) < 2 or hi.mean() - lo.mean() < separation * lo.std():
        return {"labels": [None] * len(images), "clusters": 0, "background_ncc": round(med, 4),
                "reason": "no separable fingerprints (correlations are not bimodal)"}
    # union-find over strong links
    parent = list(range(len(idx)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    ii, jj = np.where(np.triu(C > thr, 1))
    for a, b in zip(ii, jj):
        parent[find(a)] = find(b)
    comps = {}
    for a in range(len(idx)):
        comps.setdefault(find(a), []).append(a)
    groups = [g for g in comps.values() if len(g) >= min_size]
    if len(groups) < 2:
        return {"labels": [None] * len(images), "clusters": len(groups), "threshold": thr,
                "background_ncc": med, "reason": "no separable fingerprints"}
    for _ in range(2):                       # refine against cluster fingerprints
        F = np.stack([X[g].mean(axis=0) for g in groups])
        F /= np.linalg.norm(F, axis=1, keepdims=True)
        S = X @ F.T
        best = S.argmax(axis=1)
        srt = np.sort(S, axis=1)
        margin = srt[:, -1] - srt[:, -2]
        groups = [[a for a in range(len(idx)) if best[a] == c and margin[a] > 0.01] for c in range(len(groups))]
        groups = [g for g in groups if len(g) >= min_size]
        if len(groups) < 2:
            return {"labels": [None] * len(images), "clusters": len(groups), "reason": "clusters collapsed"}
    labels = [None] * len(images)
    for c, g in enumerate(groups):
        for a in g:
            labels[idx[a]] = c
    intra = [float(np.mean(C[np.ix_(g, g)][~np.eye(len(g), dtype=bool)])) for g in groups]
    return {"labels": labels, "clusters": len(groups), "sizes": [len(g) for g in groups],
            "threshold": round(thr, 4), "background_ncc": round(med, 4),
            "intra_cluster_ncc": [round(v, 4) for v in intra],
            "unassigned": sum(1 for l in labels if l is None) - (len(images) - len(idx))}
