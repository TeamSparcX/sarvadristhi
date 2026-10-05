"""Video-evidence cases: a USB stick / SD card image, a folder of exports, or video files.

  image of removable media -> fsrecover: list live + deleted files, date deletions from the
                              NTFS change journal, recover and verify deleted files
  every video file          -> hash, normalise to MP4, read camera label + clock from the
                              burned-in overlay (OCR), motion triage, AI layers L1-L4
  case                      -> findings, timeline, custody ledger, HTML/PDF report"""
import hashlib
import json
import shutil
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, fsrecover, ocr, report
from .acquire import hash_file
from .custody import Ledger
from .pipeline import LIMITATIONS, _tool_versions

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".dav", ".h264", ".264", ".h265", ".265", ".hevc", ".ts", ".mpg"}
VIDEO_SIGNATURES = {"MP4/MOV (ISO BMFF)", "RIFF (AVI/WAV)", "Matroska/WebM", "Dahua DHAV", "MPEG-PS"}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _probe(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=codec_name,width,height,avg_frame_rate,nb_read_packets:format=duration,format_name",
                        "-of", "json", str(p)], capture_output=True, text=True, encoding="utf-8")
    try:
        j = json.loads(r.stdout)
        s = j["streams"][0]
        num, den = (s.get("avg_frame_rate") or "0/1").split("/")
        return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
                "fps": round(float(num) / float(den), 3) if float(den) else None,
                "frames": int(s.get("nb_read_packets") or 0), "duration_s": float(j["format"].get("duration") or 0)}
    except (ValueError, KeyError, IndexError):
        return {}


def _overlay(mp4: Path, seconds: int = 8) -> dict:
    """Camera label and clock from the burned-in overlay of the first seconds."""
    import cv2
    import numpy as np
    pr = _probe(mp4)
    w, h = pr.get("width"), pr.get("height")
    if not w or not ocr.available():
        return {"read": False}
    r = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(mp4), "-t", str(seconds), "-vf", "fps=1", "-f", "rawvideo",
                        "-pix_fmt", "bgr24", "-"], capture_output=True)
    n = w * h * 3
    imgs = [np.frombuffer(r.stdout[k:k + n], np.uint8).reshape(h, w, 3) for k in range(0, len(r.stdout) - n + 1, n)]
    if not imgs:
        return {"read": False}
    res, rois = ocr.read_overlays(imgs)
    label = ocr.consensus_label([x["label"] for x in res if x["label"]])
    chans = [x["channel"] for x in res if x["channel"] is not None]
    starts = [x["clock_s"] - k for k, x in enumerate(res) if x["clock_s"]]
    start = statistics.median(starts) if starts else None
    agree = sum(1 for s in starts if start is not None and abs(s - start) <= 1)
    return {"read": True, "label": label, "channel": Counter(chans).most_common(1)[0][0] if chans else None,
            "start_s": start, "clock_readings": len(starts), "clock_agreeing": agree, "frames_read": len(imgs),
            "rois": rois, "raw": [{"label": x["label"], "clock": x["clock_s"]} for x in res]}


def _normalise(src: Path, dst: Path) -> dict:
    pr = _probe(src)
    if pr.get("codec") in ("h264", "hevc") and src.suffix.lower() in (".mp4", ".mov"):
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
        how = "stream copy (no re-encode)"
    else:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "fast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
        how = f"transcoded from {pr.get('codec')} to H.264 for playback (original kept and hashed)"
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return {"ok": r.returncode == 0 and dst.exists(), "how": how, "error": r.stderr[-300:]}


def run(source: str, case_dir: Path, case_id: str, examiner: str, *, ai: bool = True, analytics: bool = True,
        pdf: bool = True, log=print) -> dict:
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    led = Ledger(case_dir / "custody.jsonl")
    led.append("case-opened", case_id=case_id, examiner=examiner, tool=f"Sarvadristhi {__version__}", kind="video-evidence")
    src = Path(source)
    findings, fs, videos = [], None, []
    files_dir = case_dir / "files"
    files_dir.mkdir(exist_ok=True)

    if src.is_file() and src.suffix.lower() not in VIDEO_EXT:
        log("::stage:: acquire")
        log("Hashing evidence image ...")
        before = hash_file(src)
        led.append("evidence-registered", path=str(src), **before)
        log("::stage:: identify")
        log("Reading filesystem and change journal ...")
        log("::stage:: recover")
        fs = fsrecover.analyse(str(src), case_dir, led, log)
        for d in fs["deleted"]:
            sev = "critical"
            findings.append({"kind": "deleted-file", "severity": sev,
                             "summary": f"{d['path']} was deleted"
                                        + (f" at {d['deleted_at'][:19].replace('T', ' ')} UTC" if d.get("deleted_at") else "")
                                        + f": {d.get('verdict', 'not recovered')}",
                             "detail": {"path": d["path"], "deleted_at": d.get("deleted_at"),
                                        "basis": d.get("deleted_at_basis"), "verdict": d.get("verdict"),
                                        "sha256": (d.get("hash") or {}).get("sha256")}})
            if d.get("recovered_file") and Path(d["name"]).suffix.lower() in VIDEO_EXT and d.get("recoverable"):
                videos.append({"path": case_dir / d["recovered_file"], "name": d["name"], "provenance": "deleted-recovered",
                               "provenance_detail": f"deleted {d.get('deleted_at') or '(time unknown)'}; {d['deleted_at_basis']}"})
        vol = fsrecover.Volume(str(src))
        # a live file whose header says video but whose name says otherwise is a disguised video
        for f in fs["files"]:
            if f["deleted"] or f["dir"] or not f["size"] or f["inode"] is None or \
                    Path(f["name"]).suffix.lower() in VIDEO_EXT:
                continue
            try:
                head = vol.fs.open_meta(inode=f["inode"]).read_random(0, 64)
            except OSError:
                continue
            sig = fsrecover._signature(head)
            if sig in VIDEO_SIGNATURES:
                ext = {"MP4/MOV (ISO BMFF)": ".mp4", "RIFF (AVI/WAV)": ".avi", "Matroska/WebM": ".mkv",
                       "Dahua DHAV": ".dav", "MPEG-PS": ".ps"}[sig]
                dst = files_dir / f"{f['inode']}_{f['name']}{ext}"
                if not dst.exists():
                    vol.read(f["inode"], f["size"], dst)
                findings.append({"kind": "disguised-file", "severity": "critical",
                                 "summary": f"{f['path']} is named as '{Path(f['name']).suffix or 'no extension'}' "
                                            f"but its header is {sig}: a disguised video",
                                 "detail": {"path": f["path"], "signature": sig, "header_hex": head[:16].hex(),
                                            "modified": f["modified"]}})
                videos.append({"path": dst, "name": f["name"], "provenance": "disguised-file",
                               "provenance_detail": f"{f['path']}: extension says text, header says {sig}"})
        for f in fs["files"]:
            if not f["deleted"] and not f["dir"] and Path(f["name"]).suffix.lower() in VIDEO_EXT:
                dst = files_dir / f"{f['inode']}_{f['name']}"
                if not dst.exists():
                    vol.read(f["inode"], f["size"], dst)
                videos.append({"path": dst, "name": f["name"], "provenance": "live-file",
                               "provenance_detail": f"allocated file {f['path']}, modified {f['modified']}"})
        led.append("files-extracted", live=sum(v["provenance"] == "live-file" for v in videos),
                   recovered=sum(v["provenance"] == "deleted-recovered" for v in videos))
    else:
        items = sorted(p for p in (src.iterdir() if src.is_dir() else [src]) if p.suffix.lower() in VIDEO_EXT)
        before = {"files": {p.name: _sha(p) for p in items}}
        led.append("evidence-registered", path=str(src), files=before["files"])
        videos = [{"path": p, "name": p.name, "provenance": "live-file", "provenance_detail": f"file {p}"} for p in items]

    clips_dir = case_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    clips, events = [], []
    log("::stage:: attribute")
    for k, v in enumerate(videos, 1):
        cid = f"F{k:02d}"
        log(f"Video {cid}: {v['name']} ({v['provenance']})")
        mp4 = clips_dir / f"{cid}.mp4"
        norm = _normalise(Path(v["path"]), mp4)
        pr = _probe(mp4)
        ov = _overlay(mp4) if norm["ok"] else {"read": False}
        start_ms = int(ov["start_s"] * 1000) if ov.get("start_s") else None
        time_source = "overlay (OCR)" if start_ms else "none"
        if start_ms is None:          # fall back to the filesystem timestamp, said plainly
            log(f"  no readable clock in {cid}; time unknown")
        clip = {"id": cid, "channel": ov.get("channel"), "label": ov.get("label"), "source_file": v["name"],
                "frames": pr.get("frames", 0), "keyframes": None, "start_ms": start_ms,
                "end_ms": start_ms + int(pr.get("duration_s", 0) * 1000) if start_ms else None,
                "fps": pr.get("fps"), "evidence_level": "content-only", "levels": {"content-only": pr.get("frames", 0)},
                "time_source": [time_source], "first_offset": None, "bytes": Path(v["path"]).stat().st_size,
                "original_sha256": _sha(Path(v["path"])), "mp4_file": mp4.name if norm["ok"] else None,
                "mp4_sha256": _sha(mp4) if norm["ok"] else None, "normalised": norm["how"],
                "provenance": v["provenance"], "provenance_detail": v["provenance_detail"],
                "overlay": {k2: v2 for k2, v2 in ov.items() if k2 != "raw"}, "video": pr}
        if norm["ok"]:
            from . import integrity
            orig = Path(v["path"])
            integ = integrity.check(orig if orig.suffix.lower() in (".mp4", ".mov", ".mkv", ".ts") else mp4, fps=pr.get("fps"))
            clip["integrity"] = integ
            for fnd in integrity.findings(v["name"], integ):
                findings.append(fnd)
        clips.append(clip)
        led.append("video-registered", clip=cid, file=v["name"], provenance=v["provenance"],
                   original_sha256=clip["original_sha256"], mp4_sha256=clip["mp4_sha256"],
                   camera=clip["label"], start=start_ms)
        if analytics and norm["ok"] and start_ms:
            from .analytics.motion import detect_events
            ex = [b for b in ((ov.get("rois") or {}).get("clock"), (ov.get("rois") or {}).get("label")) if b]
            for e in detect_events(str(mp4), exclude=ex, src_width=pr.get("width")):
                events.append({**e, "channel": clip["channel"], "clip": cid,
                               "abs_start_ms": start_ms + int(e["start_s"] * 1000),
                               "abs_end_ms": start_ms + int(e["end_s"] * 1000)})
    log("::stage:: clips")
    led.append("videos-analysed", clips=len(clips), motion_events=len(events))

    ai_out = {}
    if ai:
        from . import ai as ai_layer
        log("AI layers L1-L4 ...")
        ai_out = ai_layer.run(case_dir, clips, events, findings, log=log)
        led.append("ai-analysis", tracks=len(ai_out["layers"]["L2"]["tracks"]),
                   vlm_descriptions=len(ai_out["layers"]["L3"]["descriptions"]),
                   insights=len(ai_out["layers"]["L4"]["insights"]),
                   sha256=hash_file(case_dir / "ai" / "ai.json")["sha256"])

    after = hash_file(src) if src.is_file() and src.suffix.lower() not in VIDEO_EXT else \
        {"files": {p.name: _sha(p) for p in sorted(p for p in (src.iterdir() if src.is_dir() else [src])
                                                    if p.suffix.lower() in VIDEO_EXT)}}
    led.append("post-analysis-verification", unchanged=after == before)
    cams = {}
    for c in clips:
        key = c["channel"] if c["channel"] is not None else c["label"] or c["id"]
        cam = cams.setdefault(key, {"channel": c["channel"], "label": c["label"], "clips": 0, "frames": 0, "drift": None})
        cam["clips"] += 1
        cam["frames"] += c["frames"]
    items = [{"kind": "clip", "channel": c["channel"], "start_ms": c["start_ms"], "end_ms": c["end_ms"], "id": c["id"],
              "level": c["evidence_level"]} for c in clips if c["start_ms"]]
    items += [{"kind": "event", **e, "start_ms": e["abs_start_ms"]} for e in events]
    for f in findings:
        if f["detail"].get("deleted_at"):
            items.append({"kind": "finding", "start_ms": int(datetime.fromisoformat(f["detail"]["deleted_at"]).timestamp() * 1000),
                          "text": f["summary"], "severity": f["severity"]})
    ok, status = led.verify()
    case = {
        "case": {"case_id": case_id, "examiner": examiner, "kind": "video-evidence"},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tool": {"name": "sarvadristhi", "version": __version__, "components": _tool_versions()},
        "options": {"mode": "video-evidence", "ai": ai},
        "evidence": {"source": str(src), "bytes": src.stat().st_size if src.is_file() else sum(Path(v["path"]).stat().st_size for v in videos),
                     "before": before, "after": after, "unchanged": before == after},
        "acquisition": None, "e01": None,
        "detection": {"family": f"removable media ({fs['filesystem']})" if fs else "video files",
                      "plugin": "fsrecover" if fs else "media", "confidence": 1.0, "brands_using_family": [],
                      "candidates": [], "source_bytes": src.stat().st_size if src.is_file() else None},
        "recovery": {"mode": "filesystem" if fs else "files", "plugin": "fsrecover" if fs else "media",
                     "frames": sum(c["frames"] for c in clips), "levels": {"content-only": sum(c["frames"] for c in clips)},
                     "master_sector": None, "logs": [], "index_entries": 0, "universal_sweep": None,
                     "attribution": "overlay OCR", "fragments": 0},
        "filesystem": fs, "findings": findings, "cameras": list(cams.values()), "clips": clips, "events": events,
        "ai": ai_out, "ai_triage": {}, "limitations": LIMITATIONS,
        "timeline": {"items": sorted(items, key=lambda x: x["start_ms"]),
                     "channels": sorted({c["channel"] for c in clips if c["channel"] is not None})},
    }
    led.append("results-written", findings=len(findings), clips=len(clips))
    ok, status = led.verify()
    case["custody"] = {"ok": ok, "status": status, "entries": led.entries()}
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    log("::stage:: report")
    log("Rendering report ...")
    try:
        rep = report.render(case, case_dir, pdf=pdf)
        if "pdf" in rep:
            led.append("report-generated", pdf_sha256=hash_file(case_dir / rep["pdf"])["sha256"])
    except Exception as e:      # the case data and UI do not depend on the report renderer
        rep = {"error": str(e)[:300]}
        log(f"  report failed: {e}")
    case["report"] = rep
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    return case
