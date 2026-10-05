"""What the examiner adds to a case, kept apart from machine output and logged to custody:

  Sakshilekh   findings the examiner writes, anchored to a clip interval, with the frame captured
  Kaalshodhan  examiner clock corrections (recorder clock to real-world time); originals untouched
  Khandmudra   exported segments: a clip interval cut without re-encoding, hashed, as a labelled derivative

Each record lives in its own JSON file in the case folder and every change is a custody entry, so
the analysis output (case.json) is never rewritten."""
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from .custody import Ledger


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _load(case_dir: Path, name: str) -> list:
    p = Path(case_dir) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(case_dir: Path, name: str, rows: list):
    (Path(case_dir) / name).write_text(json.dumps(rows, indent=1), encoding="utf-8")


def _clip(case_dir: Path, clip_id: str) -> dict:
    c = json.loads((Path(case_dir) / "case.json").read_text(encoding="utf-8"))
    k = next((x for x in c["clips"] if x["id"] == clip_id), None)
    if not k or not k.get("mp4_file"):
        raise ValueError(f"no playable clip {clip_id!r}")
    return k


def grab_frame(src: Path, at_s: float, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, at_s):.3f}", "-i", str(src), "-frames:v", "1",
                    "-q:v", "3", str(dst)], capture_output=True)
    return dst.exists()


# ------------------------------------------------------------------ Sakshilekh
def add_note(case_dir: Path, clip_id: str, from_s: float, to_s: float, text: str, examiner: str) -> dict:
    case_dir = Path(case_dir)
    text = (text or "").strip()
    if not text:
        raise ValueError("write the finding first")
    k = _clip(case_dir, clip_id)
    from_s, to_s = max(0.0, float(from_s)), max(float(from_s), float(to_s))
    nid = f"S{len(_load(case_dir, 'sakshilekh.json')) + 1:03d}"
    img = case_dir / "sakshilekh" / f"{nid}_{clip_id}.jpg"
    ok = grab_frame(case_dir / "clips" / k["mp4_file"], from_s, img)
    note = {"id": nid, "clip": clip_id, "camera": k.get("label"), "from_s": round(from_s, 3), "to_s": round(to_s, 3),
            "recorder_from_ms": (k["start_ms"] + int(from_s * 1000)) if k.get("start_ms") else None,
            "recorder_to_ms": (k["start_ms"] + int(to_s * 1000)) if k.get("start_ms") else None,
            "text": text[:2000], "examiner": (examiner or "")[:80], "utc": _utc(),
            "frame": f"sakshilekh/{img.name}" if ok else None, "frame_sha256": _sha(img) if ok else None,
            "clip_mp4_sha256": k.get("mp4_sha256")}
    rows = _load(case_dir, "sakshilekh.json") + [note]
    _save(case_dir, "sakshilekh.json", rows)
    Ledger(case_dir / "custody.jsonl").append("sakshilekh-finding", **{x: note[x] for x in
                                              ("id", "clip", "from_s", "to_s", "text", "examiner", "frame_sha256")})
    return note


def notes(case_dir: Path) -> list:
    return _load(case_dir, "sakshilekh.json")


# ------------------------------------------------------------------ Kaalshodhan
def set_offset(case_dir: Path, evidence: str, offset_s: float, reason: str, examiner: str,
               reference: str = "") -> dict:
    """Recorder clock correction for one evidence item: real-world time = recorder time + offset.
    The newest entry per item applies; earlier ones stay in the file and in custody."""
    case_dir = Path(case_dir)
    if not (reason or "").strip():
        raise ValueError("state how the offset was established (e.g. recorder clock vs NTP at seizure)")
    row = {"evidence": evidence, "offset_s": round(float(offset_s), 3), "reason": reason.strip()[:500],
           "reference": (reference or "")[:200], "examiner": (examiner or "")[:80], "utc": _utc()}
    rows = _load(case_dir, "kaalshodhan.json") + [row]
    _save(case_dir, "kaalshodhan.json", rows)
    Ledger(case_dir / "custody.jsonl").append("kaalshodhan-offset", **row)
    return row


def offsets(case_dir: Path) -> dict:
    """{'current': {evidence: row}, 'history': [...]}"""
    rows = _load(case_dir, "kaalshodhan.json")
    cur = {}
    for r in rows:
        cur[r["evidence"]] = r
    return {"current": cur, "history": rows}


# ------------------------------------------------------------------ Khandmudra
def export_segment(case_dir: Path, clip_id: str, from_s: float, to_s: float, purpose: str, examiner: str) -> dict:
    """Cut [from_s, to_s] of a clip without re-encoding. Stream copy starts on a keyframe, so the
    actual start can be earlier than asked; both are recorded."""
    case_dir = Path(case_dir)
    k = _clip(case_dir, clip_id)
    from_s, to_s = max(0.0, float(from_s)), float(to_s)
    if to_s <= from_s:
        raise ValueError("the end must be after the start")
    sid = f"K{len(_load(case_dir, 'khandmudra.json')) + 1:03d}"
    out = case_dir / "khandmudra" / f"{sid}_{clip_id}_{from_s:.1f}-{to_s:.1f}.mp4"
    out.parent.mkdir(exist_ok=True)
    src = case_dir / "clips" / k["mp4_file"]
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{from_s:.3f}", "-to", f"{to_s:.3f}", "-i", str(src),
                        "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    if r.returncode or not out.exists():
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                         "stream=nb_read_packets:format=duration", "-of", "json", str(out)], capture_output=True, text=True)
    info = json.loads(pr.stdout or "{}")
    row = {"id": sid, "clip": clip_id, "camera": k.get("label"), "asked_from_s": from_s, "asked_to_s": to_s,
           "duration_s": float(info.get("format", {}).get("duration") or 0),
           "frames": int((info.get("streams") or [{}])[0].get("nb_read_packets") or 0),
           "recorder_from_ms": (k["start_ms"] + int(from_s * 1000)) if k.get("start_ms") else None,
           "file": f"khandmudra/{out.name}", "sha256": _sha(out), "source_mp4_sha256": k.get("mp4_sha256"),
           "method": "ffmpeg stream copy (no re-encode); starts at the nearest earlier keyframe",
           "purpose": (purpose or "")[:300], "examiner": (examiner or "")[:80], "utc": _utc()}
    rows = _load(case_dir, "khandmudra.json") + [row]
    _save(case_dir, "khandmudra.json", rows)
    Ledger(case_dir / "custody.jsonl").append("khandmudra-segment", **{x: row[x] for x in
                                              ("id", "clip", "asked_from_s", "asked_to_s", "frames", "sha256",
                                               "source_mp4_sha256", "purpose", "examiner")})
    return row


def segments(case_dir: Path) -> list:
    return _load(case_dir, "khandmudra.json")


# ------------------------------------------------------------------ report frames
def report_frames(case_dir: Path, c: dict) -> list:
    """One still per camera (and per critical finding that names a clip time) for the report,
    written to report_frames/ with their hashes."""
    case_dir = Path(case_dir)
    out, seen = [], set()
    for k in c["clips"]:
        if not k.get("mp4_file") or k.get("label") in seen:
            continue
        seen.add(k.get("label"))
        at = min(3.0, max(0.0, ((k.get("end_ms") or 0) - (k.get("start_ms") or 0)) / 2000))
        img = case_dir / "report_frames" / f"{k['id']}.jpg"
        if img.exists() or grab_frame(case_dir / "clips" / k["mp4_file"], at, img):
            out.append({"clip": k["id"], "camera": k.get("label"), "file": f"report_frames/{img.name}",
                        "sha256": _sha(img), "at_s": at,
                        "recorder_ms": (k["start_ms"] + int(at * 1000)) if k.get("start_ms") else None,
                        "clip_sha256": k.get("mp4_sha256"), "level": k.get("evidence_level")})
    return out
