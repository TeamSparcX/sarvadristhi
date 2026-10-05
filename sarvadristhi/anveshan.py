"""Anveshan, Avaran and Saakshyapotli: three examiner tools over a finished case.

  Anveshan       find a person across every camera of every evidence item. A chosen person track
                 is compared with all other person tracks by clothing-colour signatures and, when
                 both crops show a face, by SFace face embeddings. Results are RANKED LEADS with
                 every factor shown; a match is never an identification.
  Avaran         a redacted copy of a clip for disclosure: every tracked person is blurred except
                 the ones the examiner chooses to keep. The original clip is never modified; the
                 redacted file is hashed and logged with its parameters.
  Saakshyapotli  one sealed evidence package to hand over: report, case data, custody ledgers,
                 clips, capsule, examiner work and redactions, with a SHA-256 manifest signed by
                 the examiner key (Ed25519). Its hash goes into the custody ledger.

Signatures for Anveshan are computed from the track crops the AI layer already saved and are
cached outside the case folder (cases/.cache), so the case itself stays unchanged."""
import hashlib
import json
import os
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np

from .custody import Ledger

FACE_SAME = 0.363          # SFace cosine threshold for "same person" (OpenCV zoo)
DISCLAIMER = ("Ranked leads for an examiner: colour signatures and face similarity can match different people "
              "and miss the same person. Not an identification.")


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _ai(case_dir: Path) -> dict:
    p = Path(case_dir) / "ai" / "ai.json"
    if not p.exists():
        raise ValueError("this case has no AI results: run the analysis with AI layers on")
    return json.loads(p.read_text(encoding="utf-8"))


def _load(case_dir: Path, name: str) -> list:
    p = Path(case_dir) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(case_dir: Path, name: str, rows: list):
    (Path(case_dir) / name).write_text(json.dumps(rows, indent=1), encoding="utf-8")


# ======================================================================= Anveshan
def _signatures(case_dir: Path, tracks: list[dict]) -> dict:
    """track id -> {upper, lower histograms, face embedding or None}, cached per ai.json version."""
    from .ai import perception
    case_dir = Path(case_dir)
    ai_sha = _sha(case_dir / "ai" / "ai.json")[:16]
    cache = case_dir.parent / ".cache" / f"anveshan_{case_dir.name}_{ai_sha}.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return z["sigs"].item()
    faces = perception.Faces()
    sigs = {}
    for t in tracks:
        img = cv2.imread(str(case_dir / t.get("crop", "")))
        if img is None or img.shape[0] < 24 or img.shape[1] < 10:
            continue
        h, w = img.shape[:2]
        a = perception.appearance(img, [min(12, w // 6), min(12, h // 8), w - min(12, w // 6), h - min(12, h // 8)])
        if not a:
            continue
        emb = None
        if faces.ok:                         # faces are small in CCTV crops: upscale before detecting
            big = cv2.resize(img, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
            fs = faces(big)
            if fs:
                emb = max(fs, key=lambda f: f["conf"])["embedding"]
        sigs[t["id"]] = {"upper": a["upper"], "lower": a["lower"], "upper_colour": a["upper_colour"],
                         "lower_colour": a["lower_colour"], "face": emb}
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, sigs=np.array(sigs, dtype=object))
    return sigs


def _hist(a, b) -> float:
    return max(0.0, float(cv2.compareHist(a.astype(np.float32), b.astype(np.float32), cv2.HISTCMP_CORREL)))


def search(case_dir: Path, track_id: str, limit: int = 24) -> dict:
    """Rank every other person track of the case by similarity to `track_id`."""
    case_dir = Path(case_dir)
    ai = _ai(case_dir)
    tracks = [t for t in ai["layers"]["L2"]["tracks"] if t["cls"] == "person"]
    by_id = {t["id"]: t for t in tracks}
    if track_id not in by_id:
        raise ValueError(f"no person track {track_id}")
    sigs = _signatures(case_dir, tracks)
    q, qs = by_id[track_id], sigs.get(track_id)
    if not qs:
        raise ValueError("this track's crop is too small to compare")
    entity_of = {t: e["id"] for e in ai["layers"]["L4"].get("entities", []) for t in e["tracks"]}
    out, excluded = [], 0
    for t in tracks:
        if t["id"] == track_id or t["id"] not in sigs:
            continue
        s = sigs[t["id"]]
        # one person cannot be two tracks on the same camera at the same time
        if t["channel_name"] == q["channel_name"] and t["abs_start_ms"] < q["abs_end_ms"] and q["abs_start_ms"] < t["abs_end_ms"]:
            excluded += 1
            continue
        up, lo = _hist(qs["upper"], s["upper"]), _hist(qs["lower"], s["lower"])
        app = 0.6 * up + 0.4 * lo
        factors = [{"factor": "upper clothing colour", "value": round(up, 2),
                    "why": f"{qs['upper_colour']} vs {s['upper_colour']}"},
                   {"factor": "lower clothing colour", "value": round(lo, 2),
                    "why": f"{qs['lower_colour']} vs {s['lower_colour']}"}]
        if qs["face"] is not None and s["face"] is not None:
            cos = float(np.dot(qs["face"], s["face"]) / (np.linalg.norm(qs["face"]) * np.linalg.norm(s["face"]) + 1e-9))
            fv = float(np.clip((cos - 0.10) / (FACE_SAME - 0.10), 0, 1))
            factors.append({"factor": "face (SFace cosine)", "value": round(fv, 2),
                            "why": f"cosine {cos:.3f}; same-person threshold {FACE_SAME}"})
            score = 0.55 * fv + 0.45 * app
        else:
            factors.append({"factor": "face", "value": None, "why": "no face visible in both crops"})
            score = app
        band = "strong lead" if score >= 0.75 else "possible" if score >= 0.6 else "weak"
        out.append({"track": t["id"], "clip": t["clip"], "camera": t["channel_name"], "score": round(score, 3),
                    "band": band, "factors": factors, "from_ms": t["abs_start_ms"], "to_ms": t["abs_end_ms"],
                    "best_ms": t["abs_best_ms"], "crop": t["crop"], "thumb": t["thumb"],
                    "same_l4_entity": entity_of.get(t["id"]) is not None and entity_of.get(t["id"]) == entity_of.get(track_id)})
    out.sort(key=lambda r: -r["score"])
    return {"query": {"track": track_id, "camera": q["channel_name"], "from_ms": q["abs_start_ms"],
                      "to_ms": q["abs_end_ms"], "crop": q["crop"], "upper_colour": qs["upper_colour"],
                      "lower_colour": qs["lower_colour"], "face": qs["face"] is not None},
            "results": out[:limit], "compared": len(out), "excluded_same_camera_same_time": excluded,
            "cameras": sorted({r["camera"] for r in out[:limit] if r["band"] != "weak"}),
            "method": "clothing-colour histograms (upper 0.6, lower 0.4) and, when both crops show a face, "
                      "SFace cosine (face 0.55, colour 0.45); same camera at the same time excluded",
            "disclaimer": DISCLAIMER}


def candidates(case_dir: Path, limit: int = 48) -> list[dict]:
    """The strongest person tracks, to pick a subject from."""
    ai = _ai(case_dir)
    ts = [t for t in ai["layers"]["L2"]["tracks"] if t["cls"] == "person"]
    ts.sort(key=lambda t: -(t["observations"] * t["max_conf"]))
    return [{"track": t["id"], "camera": t["channel_name"], "clip": t["clip"], "crop": t["crop"],
             "from_ms": t["abs_start_ms"], "to_ms": t["abs_end_ms"], "observations": t["observations"],
             "colours": f"{t.get('upper_colour') or '?'} / {t.get('lower_colour') or '?'}",
             "face": bool((t.get("face") or {}).get("found"))} for t in ts[:limit]]


# ======================================================================= Avaran
def _boxes_at(samples: list, t: float, pad: float = 0.15):
    """Interpolated box of one track at time t from its (t, box) samples; None outside its span."""
    if t < samples[0][0] - 0.3 or t > samples[-1][0] + 0.3:
        return None
    prev = max((s for s in samples if s[0] <= t), default=samples[0], key=lambda s: s[0])
    nxt = min((s for s in samples if s[0] >= t), default=samples[-1], key=lambda s: s[0])
    if nxt[0] == prev[0]:
        b = prev[1]
    else:
        k = (t - prev[0]) / (nxt[0] - prev[0])
        b = [p + (n - p) * k for p, n in zip(prev[1], nxt[1])]
    w, h = b[2] - b[0], b[3] - b[1]
    return [b[0] - pad * w, b[1] - pad * h, b[2] + pad * w, b[3] + pad * h]


def redact(case_dir: Path, clip_id: str, keep: list[str], examiner: str = "", purpose: str = "") -> dict:
    """Write a copy of `clip_id` with every tracked person blurred except the tracks in `keep`."""
    case_dir = Path(case_dir)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    clip = next((k for k in case["clips"] if k["id"] == clip_id), None)
    if not clip or not clip.get("mp4_file"):
        raise ValueError(f"no playable clip {clip_id}")
    ai = _ai(case_dir)
    dets = next((p["detections"] for p in ai["layers"]["L2"]["clips"] if p["clip"] == clip_id), [])
    tracks = {}
    for d in dets:
        if d["cls"] == "person":
            tracks.setdefault(d["track"], []).append((d["t"], d["box"]))
    for v in tracks.values():
        v.sort(key=lambda s: s[0])
    keep = set(keep or [])
    src = case_dir / "clips" / clip["mp4_file"]
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or float(clip.get("fps") or 10)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    rows = _load(case_dir, "avaran.json")
    rid = f"A{len(rows) + 1:03d}"
    out = case_dir / "avaran" / f"{rid}_{clip_id}_redacted.mp4"
    out.parent.mkdir(exist_ok=True)
    src_sha = clip.get("mp4_sha256") or _sha(src)
    blurred = frames = 0
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "redacted.avi"
        wr = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
        k = 0
        while True:
            ok, img = cap.read()
            if not ok:
                break
            t = k / fps
            for tid, samples in tracks.items():
                if tid in keep:
                    continue
                b = _boxes_at(samples, t)
                if not b:
                    continue
                x0, y0 = max(0, int(b[0])), max(0, int(b[1]))
                x1, y1 = min(w, int(b[2])), min(h, int(b[3]))
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue
                roi = img[y0:y1, x0:x1]
                small = cv2.resize(roi, (max(1, (x1 - x0) // 12), max(1, (y1 - y0) // 12)), interpolation=cv2.INTER_LINEAR)
                img[y0:y1, x0:x1] = cv2.GaussianBlur(cv2.resize(small, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST), (0, 0), 3)
                blurred += 1
            cap_txt = f"REDACTED COPY {rid} | source sha256 {src_sha[:16]} | {len(keep)} kept"
            cv2.rectangle(img, (0, h - 26), (min(w, 12 + 9 * len(cap_txt)), h), (0, 0, 0), -1)
            cv2.putText(img, cap_txt, (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            wr.write(img)
            k += 1
            frames += 1
        wr.release()
        cap.release()
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c:v", "libx264", "-threads", "1",
                            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        if r.returncode or not out.exists():
            raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    row = {"id": rid, "clip": clip_id, "camera": clip.get("label"), "file": f"avaran/{out.name}", "sha256": _sha(out),
           "source_mp4_sha256": src_sha, "kept_tracks": sorted(keep),
           "blurred_tracks": sorted(t for t in tracks if t not in keep), "frames": frames, "boxes_blurred": blurred,
           "method": "tracked person boxes from AI layer 2, interpolated between samples, +15% margin, "
                     "pixelate x12 + Gaussian blur; re-encoded H.264 CRF 20; original clip unchanged",
           "limits": "people the detector never tracked are NOT blurred: review the copy frame by frame before release",
           "purpose": (purpose or "")[:300], "examiner": (examiner or "")[:80], "utc": _utc()}
    _save(case_dir, "avaran.json", rows + [row])
    Ledger(case_dir / "custody.jsonl").append("avaran-redacted-copy", **{x: row[x] for x in
                                              ("id", "clip", "sha256", "source_mp4_sha256", "kept_tracks", "frames",
                                               "boxes_blurred", "purpose", "examiner")})
    return row


def redactions(case_dir: Path) -> list:
    return _load(case_dir, "avaran.json")


# ======================================================================= Saakshyapotli
PACKAGE_PARTS = ["report.pdf", "report.html", "case.json", "custody.jsonl", "frames.csv", "clips", "evidence",
                 "capsule", "khandmudra", "avaran", "ai/ai.json", "reviews.json", "notes.json", "khandmudra.json",
                 "avaran.json", "offsets.json"]
VERIFY_README = """SAAKSHYAPOTLI - sealed evidence package ({case})
Sealed {utc} by {examiner} with Sarvadristhi. Key fingerprint {fp}.

What is inside: the case report (PDF/HTML), the case data (case.json), the hash-chained custody
ledgers, every recovered clip (MP4 and raw elementary stream), the replay capsule, examiner notes,
segments and redacted copies, and the AI results (leads only, never findings).

How to verify:
1. MANIFEST.json lists the SHA-256 of every file. Recompute each (PowerShell: Get-FileHash <file>)
   and compare. Any difference means the file changed after sealing.
2. MANIFEST.sig is an Ed25519 signature over MANIFEST.json by the public key in MANIFEST.json.
   Verify with any Ed25519 tool, or: python -m sarvadristhi.anveshan verify <unzipped folder>
3. The package's own SHA-256 was written to the case custody ledger when it was sealed
   (action "saakshyapotli-sealed").
"""


def package(case_dir: Path, examiner: str = "") -> dict:
    from . import ed25519
    case_dir = Path(case_dir)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    files = []
    for part in PACKAGE_PARTS:
        p = case_dir / part
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file())
    secret, public = ed25519.examiner_key()
    manifest = {"format": "saakshyapotli/1", "case_id": case["case"]["case_id"], "sealed_utc": _utc(),
                "examiner": (examiner or case["case"].get("examiner") or "")[:80], "tool": "Sarvadristhi",
                "public_key": public.hex(), "key_fingerprint": ed25519.fingerprint(public),
                "files": {f.relative_to(case_dir).as_posix(): {"bytes": f.stat().st_size, "sha256": _sha(f)} for f in files}}
    mbytes = json.dumps(manifest, indent=1, sort_keys=True).encode()
    sig = ed25519.sign(secret, mbytes)
    out_dir = case_dir / "potli"
    out_dir.mkdir(exist_ok=True)
    stamp = manifest["sealed_utc"].replace(":", "").replace("-", "")
    out = out_dir / f"{case_dir.name}_saakshyapotli_{stamp}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr("MANIFEST.json", mbytes)
        z.writestr("MANIFEST.sig", sig.hex())
        z.writestr("README_VERIFY.txt", VERIFY_README.format(case=manifest["case_id"], utc=manifest["sealed_utc"],
                                                            examiner=manifest["examiner"] or "the examiner",
                                                            fp=manifest["key_fingerprint"]).replace("\n", "\r\n"))
        for f in files:
            z.write(f, f.relative_to(case_dir).as_posix())
    row = {"file": f"potli/{out.name}", "sha256": _sha(out), "bytes": out.stat().st_size, "files": len(files),
           "manifest_sha256": hashlib.sha256(mbytes).hexdigest(), "key_fingerprint": manifest["key_fingerprint"],
           "sealed_utc": manifest["sealed_utc"], "examiner": manifest["examiner"]}
    rows = _load(case_dir, "potli.json") + [row]
    _save(case_dir, "potli.json", rows)
    Ledger(case_dir / "custody.jsonl").append("saakshyapotli-sealed", **row)
    return row


def packages(case_dir: Path) -> list:
    return _load(case_dir, "potli.json")


def verify_package(folder: Path) -> dict:
    """Check an unzipped package: every file hash and the manifest signature."""
    from . import ed25519
    folder = Path(folder)
    mbytes = (folder / "MANIFEST.json").read_bytes()
    m = json.loads(mbytes)
    sig_ok = ed25519.verify(bytes.fromhex(m["public_key"]), mbytes, bytes.fromhex((folder / "MANIFEST.sig").read_text().strip()))
    bad = [n for n, e in m["files"].items() if not (folder / n).exists() or _sha(folder / n) != e["sha256"]]
    return {"signature_ok": sig_ok, "files": len(m["files"]), "mismatched_or_missing": bad, "ok": sig_ok and not bad}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "verify":
        r = verify_package(Path(sys.argv[2]))
        print(json.dumps(r, indent=1))
        sys.exit(0 if r["ok"] else 1)
    print("usage: python -m sarvadristhi.anveshan verify <unzipped package folder>")
