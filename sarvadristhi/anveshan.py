"""Person search, Redacted copy and Evidence package: three examiner tools over a finished case.

  Person search       find a person across every camera of every evidence item. A chosen person track
                 is joined with its own tracker breaks (same camera, same spot, same clothes) and
                 compared with every other person by clothing colour judged against the case and,
                 when both crops hold the person's own face, by SFace face embeddings. One row per
                 person; RANKED LEADS with every factor shown; a match is never an identification.
  Redacted copy         a redacted copy of a clip for disclosure: every tracked person is blurred except
                 the ones the examiner chooses to keep. The original clip is never modified; the
                 redacted file is hashed and logged with its parameters.
  Evidence package  one sealed evidence package to hand over: report, case data, custody ledgers,
                 clips, capsule, examiner work and redactions, with a SHA-256 manifest signed by
                 the examiner key (Ed25519). Its hash goes into the custody ledger.

Signatures for Person search are computed from the track crops the AI layer already saved and are
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


# ======================================================================= Person search
FACE_RAMP = (0.25, 0.50)   # SFace cosine on small CCTV faces: 0.25 -> no support, 0.50 -> full support
FACE_DIFFERS = 0.20        # both faces clear and cosine below this: the faces disagree
LINK_GAP_S, LINK_IOU, LINK_COLOUR = 120, 0.45, 0.60                  # tracker break: same spot, same clothes
LINK_LONG_GAP_S, LINK_LONG_IOU, LINK_LONG_COLOUR = 900, 0.60, 0.75   # next clip, still in the same seat


def _own_face(img, faces):
    """The face that belongs to the person of this crop, or None. A crop of someone in a crowd often
    contains a neighbour's face; only a face centred near the top of the crop is this person's."""
    h, w = img.shape[:2]
    best = None
    for f in faces(cv2.resize(img, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)):
        x0, y0, x1, y1 = [v / 3 for v in f["box"]]
        cx, cy = (x0 + x1) / 2 / w, (y0 + y1) / 2 / h
        if f["conf"] >= 0.8 and cy <= 0.4 and 0.2 <= cx <= 0.8 and x1 - x0 >= 14:
            if best is None or f["conf"] > best["conf"]:
                best = {"conf": f["conf"], "px": round(x1 - x0), "emb": f["embedding"]}
    return best


def _signatures(case_dir: Path, tracks: list[dict]) -> dict:
    """track id -> {upper, lower histograms, owned face embedding or None}, cached per ai.json version."""
    from .ai import perception
    case_dir = Path(case_dir)
    ai_sha = _sha(case_dir / "ai" / "ai.json")[:16]
    cache = case_dir.parent / ".cache" / f"anveshan2_{case_dir.name}_{ai_sha}.npz"
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
        f = _own_face(img, faces) if faces.ok else None
        emb = None if f is None else f["emb"] / (np.linalg.norm(f["emb"]) + 1e-9)
        sigs[t["id"]] = {"upper": a["upper"], "lower": a["lower"], "upper_colour": a["upper_colour"],
                         "lower_colour": a["lower_colour"], "face": emb, "face_px": f and f["px"]}
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, sigs=np.array(sigs, dtype=object))
    return sigs


def _hist(a, b) -> float:
    return max(0.0, float(cv2.compareHist(a.astype(np.float32), b.astype(np.float32), cv2.HISTCMP_CORREL)))


def _colour(a, b) -> float:
    return 0.6 * _hist(a["upper"], b["upper"]) + 0.4 * _hist(a["lower"], b["lower"])


def _cos(a, b):
    return None if a["face"] is None or b["face"] is None else float(np.dot(a["face"], b["face"]))


def _iou(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    i = max(0, x1 - x0) * max(0, y1 - y0)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _same_shape(a, b) -> bool:
    """A seated person and someone walking through the same spot have very different boxes."""
    wa, ha, wb, hb = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
    if min(wa, ha, wb, hb) <= 0:
        return False
    area, aspect = (wa * ha) / (wb * hb), (wa / ha) / (wb / hb)
    return 0.6 <= area <= 1 / 0.6 and 1 / 1.4 <= aspect <= 1.4


def _chains(tracks: list[dict], sigs: dict):
    """Join tracker breaks: on one camera, a track that starts where another ended, in the same clothes,
    is the same person continued. Each track gets at most one predecessor and one successor (nearest
    in time and place first), so a crowd does not collapse into one chain. Returns (track -> chain id, links)."""
    pairs = []
    for a in tracks:
        for b in tracks:
            if a is b or a["channel_name"] != b["channel_name"] or a.get("frame_size") != b.get("frame_size"):
                continue
            gap = (b["abs_start_ms"] - a["abs_end_ms"]) / 1000
            if gap < -1 or gap > LINK_LONG_GAP_S or a["id"] not in sigs or b["id"] not in sigs:
                continue
            # the join must be shape-consistent, and each side must still be the person of its own crop
            # (a track whose entry or exit box differs from its best box is an identity switch: never joined)
            if not (_same_shape(a["exit_box"], b["entry_box"]) and _same_shape(a["best_box"], a["exit_box"])
                    and _same_shape(b["entry_box"], b["best_box"])):
                continue
            iou, col = _iou(a["exit_box"], b["entry_box"]), _colour(sigs[a["id"]], sigs[b["id"]])
            cos = _cos(sigs[a["id"]], sigs[b["id"]])
            if cos is not None and cos < FACE_DIFFERS:
                continue
            if (gap <= LINK_GAP_S and iou >= LINK_IOU and col >= LINK_COLOUR) or \
               (iou >= LINK_LONG_IOU and col >= LINK_LONG_COLOUR):
                pairs.append((max(0.0, gap) / LINK_GAP_S + (1 - iou), iou, a["id"], b["id"], round(gap, 1), round(col, 2)))
    succ, pred, links = {}, {}, []
    for _, iou, a, b, gap, col in sorted(pairs):
        if a in succ or b in pred:
            continue
        succ[a], pred[b] = b, a
        links.append({"from": a, "to": b, "gap_s": gap, "overlap": round(iou, 2), "colour": col})
    chain = {}
    for t in tracks:
        if t["id"] in pred:
            continue
        x = t["id"]
        while x is not None and x not in chain:
            chain[x] = t["id"]
            x = succ.get(x)
    for t in tracks:                       # (cycles are impossible: time only moves forward)
        chain.setdefault(t["id"], t["id"])
    return chain, links


def _person(case_dir: Path):
    ai = _ai(case_dir)
    tracks = [t for t in ai["layers"]["L2"]["tracks"] if t["cls"] == "person"]
    sigs = _signatures(case_dir, tracks)
    chain, links = _chains(tracks, sigs)
    groups = {}
    for t in tracks:
        groups.setdefault(chain[t["id"]], []).append(t)
    for g in groups.values():
        g.sort(key=lambda t: t["abs_start_ms"])
    return ai, tracks, sigs, chain, links, groups


def _rep(members: list[dict], sigs: dict) -> dict:
    """The member to show for a person: a clear face first, then the longest, most confident track."""
    return max(members, key=lambda t: (sigs.get(t["id"], {}).get("face") is not None, t["observations"] * t["max_conf"]))


def search(case_dir: Path, track_id: str, limit: int = 24) -> dict:
    """Rank every other person in the case by similarity to the person of `track_id`."""
    case_dir = Path(case_dir)
    ai, tracks, sigs, chain, links, groups = _person(case_dir)
    by_id = {t["id"]: t for t in tracks}
    if track_id not in by_id:
        raise ValueError(f"no person track {track_id}")
    subj = [t for t in groups[chain[track_id]] if t["id"] in sigs]
    if not subj:
        raise ValueError("this track's crop is too small to compare")
    sub_ids = {t["id"] for t in subj}
    entity_of = {t: e["id"] for e in ai["layers"]["L4"].get("entities", []) for t in e["tracks"]}
    sub_entities = {entity_of.get(i) for i in sub_ids} - {None}
    rows, excluded = [], 0
    for t in tracks:
        if t["id"] in sub_ids or t["id"] not in sigs:
            continue
        # one person cannot be two tracks on the same camera at the same time
        if any(t["channel_name"] == q["channel_name"] and t["abs_start_ms"] < q["abs_end_ms"] and
               q["abs_start_ms"] < t["abs_end_ms"] for q in subj):
            excluded += 1
            continue
        s = sigs[t["id"]]
        qs = sigs[max(subj, key=lambda q: _colour(sigs[q["id"]], s))["id"]]
        cosines = [(c, q) for q in subj if (c := _cos(sigs[q["id"]], s)) is not None]
        rows.append({"t": t, "s": s, "qs": qs, "col": _colour(qs, s), "up": _hist(qs["upper"], s["upper"]),
                     "lo": _hist(qs["lower"], s["lower"]), "face": max(cosines, key=lambda x: x[0]) if cosines else None})
    # colour is judged against the case: matching "black / black" in a lobby full of black coats says little
    base = float(np.median([r["col"] for r in rows])) if rows else 0.0
    best = {}
    for r in rows:
        t, s, qs = r["t"], r["s"], r["qs"]
        lift = float(np.clip((r["col"] - base) / max(1e-6, 1 - base), 0, 1))
        factors = [{"factor": "upper clothing colour", "value": round(r["up"], 2), "why": f"{qs['upper_colour']} vs {s['upper_colour']}"},
                   {"factor": "lower clothing colour", "value": round(r["lo"], 2), "why": f"{qs['lower_colour']} vs {s['lower_colour']}"},
                   {"factor": "colour vs a typical person", "value": round(lift, 2),
                    "why": f"colour {r['col']:.2f} against a case median of {base:.2f}"}]
        if r["face"] is not None:
            cos, fq = r["face"]
            fv = float(np.clip((cos - FACE_RAMP[0]) / (FACE_RAMP[1] - FACE_RAMP[0]), 0, 1))
            factors.append({"factor": "face (SFace cosine)", "value": round(fv, 2),
                            "why": f"cosine {cos:.3f} on {sigs[fq['id']]['face_px']} px and {s['face_px']} px faces "
                                   f"(0.25 = no support, 0.50 = full)"})
            score = 0.6 * fv + 0.4 * lift
            if cos < FACE_DIFFERS:
                score *= 0.5
            band = "strong lead" if score >= 0.75 and cos >= FACE_SAME else "possible" if score >= 0.55 else "weak"
        else:
            factors.append({"factor": "face", "value": None, "why": "no clear face of this person in both crops"})
            score = 0.85 * lift              # colour alone is never a strong lead
            band = "possible" if score >= 0.55 else "weak"
        g = chain[t["id"]]
        if g not in best or score > best[g]["score"]:
            best[g] = {"track": t["id"], "clip": t["clip"], "camera": t["channel_name"], "score": round(score, 3),
                       "band": band, "factors": factors, "from_ms": t["abs_start_ms"], "to_ms": t["abs_end_ms"],
                       "best_ms": t["abs_best_ms"], "crop": t["crop"], "thumb": t["thumb"],
                       "same_l4_entity": bool(sub_entities) and any(entity_of.get(m["id"]) in sub_entities for m in groups[g])}
    out = []
    for g, r in best.items():
        r["person_tracks"] = [{"track": m["id"], "from_ms": m["abs_start_ms"], "to_ms": m["abs_end_ms"]} for m in groups[g]]
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    rep = _rep(subj, sigs)
    qs = sigs[rep["id"]]
    return {"query": {"track": track_id, "shown": rep["id"], "camera": rep["channel_name"],
                      "from_ms": subj[0]["abs_start_ms"], "to_ms": max(t["abs_end_ms"] for t in subj), "crop": rep["crop"],
                      "upper_colour": qs["upper_colour"], "lower_colour": qs["lower_colour"],
                      "face": any(sigs[t["id"]]["face"] is not None for t in subj),
                      "members": [{"track": t["id"], "clip": t["clip"], "from_ms": t["abs_start_ms"], "to_ms": t["abs_end_ms"]} for t in subj],
                      "links": [l for l in links if l["from"] in sub_ids]},
            "results": out[:limit], "compared": len(out), "compared_tracks": len(rows),
            "excluded_same_camera_same_time": excluded, "colour_baseline": round(base, 2),
            "cameras": sorted({r["camera"] for r in out[:limit] if r["band"] != "weak"}),
            "method": "one row per person (tracker breaks on one camera joined: same spot, same clothes). Clothing colour "
                      "(upper 0.6, lower 0.4) judged against the case median; where both crops hold the person's own "
                      "face, SFace cosine (face 0.6, colour 0.4). Colour alone is at most a possible lead. Same camera "
                      "at the same time excluded.",
            "disclaimer": DISCLAIMER}


def candidates(case_dir: Path, limit: int = 48) -> list[dict]:
    """One entry per person (tracker breaks joined), strongest first, to pick a subject from."""
    ai, tracks, sigs, chain, links, groups = _person(case_dir)
    people = [ms for ms in groups.values() if any(m["id"] in sigs for m in ms)]
    people.sort(key=lambda ms: -sum(m["observations"] * m["max_conf"] for m in ms))
    out = []
    for ms in people[:limit]:
        r = _rep([m for m in ms if m["id"] in sigs], sigs)
        s = sigs[r["id"]]
        out.append({"track": r["id"], "camera": r["channel_name"], "clip": r["clip"], "crop": r["crop"],
                    "from_ms": ms[0]["abs_start_ms"], "to_ms": max(m["abs_end_ms"] for m in ms),
                    "observations": sum(m["observations"] for m in ms), "tracks": len(ms), "members": [m["id"] for m in ms],
                    "colours": f"{s['upper_colour']} / {s['lower_colour']}", "face": s["face"] is not None})
    return out


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


# ======================================================================= Evidence package
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
