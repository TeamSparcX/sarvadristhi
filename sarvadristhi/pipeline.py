"""One case run: register (or acquire) evidence, identify, recover, attribute, export,
analyse, report. Every step is written to the custody ledger."""
import csv
import json
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, clips as clipmod, clockdrift, ocr, report, timeline
from .acquire import acquire, hash_file
from .custody import Ledger
from .recover import recover
from .source import Source, require_image

LIMITATIONS = [
    "Dahua DHFS index structures are not parsed; Dahua-family frames are carved and carry the "
    "recorder timestamp from each DHAV header (evidence level: carved + timestamp).",
    "The universal path attributes camera and time from burned-in overlays; footage without "
    "overlays is recovered as content-only. PRNU sensor-noise clustering is not yet implemented.",
    "Motion triage misses objects against full-frame moving texture (heavy rain, foliage, noise).",
]


def _tool_versions() -> dict:
    v = {}
    for name, cmd in (("ffmpeg", ["ffmpeg", "-version"]), ("tesseract", ["tesseract", "--version"])):
        if shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True)
            v[name] = (out.stdout or out.stderr).splitlines()[0].split()[-1 if name == "tesseract" else 2]
    import cv2
    v["opencv"] = cv2.__version__
    return v


def run(source: str, case_dir: Path, case_id: str, examiner: str, *, acquire_to: Path | None = None,
        acquire_format: str = "raw", mode: str = "auto", export: bool = True, analytics: bool = True,
        overlay: bool = True, pdf: bool = True, ai: bool = False, log=print) -> dict:
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    led = Ledger(case_dir / "custody.jsonl")
    led.append("case-opened", case_id=case_id, examiner=examiner, tool=f"Sarvadristhi {__version__}")

    acq, e01 = None, None
    if acquire_to and acquire_format == "e01":
        from . import ewf
        log("::stage:: acquire")
        log(f"Acquiring {source} -> {acquire_to}.E01")
        acq = ewf.acquire_e01(source, Path(acquire_to), led, case_id=case_id, examiner=examiner)
        acq.update(started_utc="see custody ledger", finished_utc="", verified=acq["ewfverify_ok"],
                   unreadable_sector_count=0, write_blocker_note="Hardware write-blocker required "
                   "for original media; software read-only is advisory.")
        source = acq["image"]
    elif acquire_to:
        log("::stage:: acquire")
        log(f"Acquiring {source} -> {acquire_to}")
        acq = acquire(source, Path(acquire_to), led)
        source = str(acquire_to)
    if str(source).lower().endswith(".e01"):
        from . import ewf
        log("Expanding E01 to a verified raw working copy ...")
        e01 = ewf.expand(Path(source), case_dir / "work" / "evidence.raw", led)
        source = e01["raw"]
    require_image(source)  # Windows: fail before hashing a whole physical drive
    log("::stage:: acquire")
    log("Hashing evidence ...")
    before = hash_file(Path(source))
    led.append("evidence-registered", path=source, **before)

    with Source(source) as src:
        log("::stage:: identify")
        log("Identifying format and recovering video ...")
        det, rec = recover(src, mode=mode, attribute=overlay, log=log)
        log("::stage:: attribute")
        led.append("recovery", family=det["family"], plugin=rec.plugin, frames=len(rec.frames))
        drift = {}
        if overlay and rec.metadata.get("mode") == "native" and ocr.available():
            log("::stage:: clocks")
            log("Reading overlay clocks for clock-drift analysis ...")
            drift, dfind = clockdrift.measure(src.buf, rec.frames)
            rec.findings += dfind
        cl = clipmod.build_clips(rec.frames)
        rois = next((v["overlay_regions"] for v in drift.values() if isinstance(v, dict) and "overlay_regions" in v),
                    rec.metadata.get("attribution", {}).get("overlay_regions") if isinstance(rec.metadata.get("attribution"), dict) else None) or {}
        events = []
        if export:
            log("::stage:: clips")
            log(f"Exporting {len(cl)} clip(s) ...")
            for c in cl:
                clipmod.export(src.buf, c, case_dir / "clips")
            led.append("clips-exported", clips={c.id: c.exported.get("mp4_sha256") for c in cl})
            if analytics:
                from .analytics.motion import detect_events
                log("::stage:: l1")
                log("Motion triage ...")
                ex = [b for b in (rois.get("clock"), rois.get("label")) if b]
                width = None
                for c in cl:
                    if "mp4_file" not in c.exported or c.start_ms is None:
                        continue
                    if width is None:
                        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                            "stream=width", "-of", "csv=p=0", str(case_dir / "clips" / c.exported["mp4_file"])],
                                           capture_output=True, text=True)
                        width = int(r.stdout.strip() or 0) or None
                    for e in detect_events(str(case_dir / "clips" / c.exported["mp4_file"]), exclude=ex, src_width=width):
                        events.append({**e, "channel": c.channel, "clip": c.id,
                                       "abs_start_ms": c.start_ms + int(e["start_s"] * 1000),
                                       "abs_end_ms": c.start_ms + int(e["end_s"] * 1000)})
        ai_out, ai_full = {}, {}
        rec.findings += timeline.recording_gaps(cl)
        if ai and export:
            from . import ai as ai_layer
            log("AI layers L1-L4 ...")
            rows = []
            for c in cl:
                r = c.summary()
                r["provenance"] = "index" if r["evidence_level"] == "index-confirmed" else "carved"
                r["provenance_detail"] = f"evidence level {r['evidence_level']}"
                rows.append(r)
            ai_full = ai_layer.run(case_dir, rows, events, [asdict(f) for f in rec.findings], log=log)
            led.append("ai-analysis", tracks=len(ai_full["layers"]["L2"]["tracks"]),
                       insights=len(ai_full["layers"]["L4"]["insights"]),
                       sha256=hash_file(case_dir / "ai" / "ai.json")["sha256"])
        with (case_dir / "frames.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["offset", "length", "channel", "time_ms", "time_source", "keyframe", "evidence_level", "family"])
            for fr in sorted(rec.frames, key=lambda x: x.offset):
                w.writerow([fr.offset, fr.length, fr.channel, fr.time_ms, fr.time_source, int(fr.keyframe),
                            fr.level.label, fr.family])

    after = hash_file(Path(source))
    led.append("post-analysis-verification", unchanged=after == before, **after)
    sev = {"critical": 0, "warning": 1, "info": 2}
    findings = sorted(rec.findings, key=lambda f: sev.get(f.severity, 3))
    for f in findings:   # put format events on the timeline
        if f.kind == "format-event":
            f.detail.setdefault("time", f.detail.get("time"))
    md = rec.metadata
    per = defaultdict(lambda: {"clips": 0, "frames": 0, "label": None})
    for c in cl:
        if c.channel is not None:
            per[c.channel]["clips"] += 1
            per[c.channel]["frames"] += len(c.frames)
            per[c.channel]["label"] = per[c.channel]["label"] or c.label
    for ch, v in per.items():
        v["label"] = v["label"] or (drift.get(ch) or {}).get("label")
    cams = [{"channel": ch, **v, "drift": {k: x for k, x in (drift.get(ch) or {}).items() if k != "overlay_regions"} or None}
            for ch, v in sorted(per.items())]
    clip_rows = [c.summary() for c in cl]
    ok, status = led.verify()
    case = {
        "case": {"case_id": case_id, "examiner": examiner},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tool": {"name": "sarvadristhi", "version": __version__, "components": _tool_versions()},
        "options": {"mode": mode, "export": export, "analytics": analytics, "overlay": overlay},
        "evidence": {"source": source, "bytes": Path(source).stat().st_size, "before": before, "after": after,
                     "unchanged": before == after},
        "acquisition": acq, "e01": e01, "detection": det,
        "recovery": {"mode": md.get("mode"), "plugin": rec.plugin, "frames": len(rec.frames),
                     "levels": dict(Counter(f.level.label for f in rec.frames)),
                     "master_sector": md.get("master_sector"), "logs": md.get("logs", []),
                     "index_entries": len(md.get("index_entries", [])),
                     "universal_sweep": md.get("universal_sweep"), "attribution": md.get("attribution"),
                     "fragments": len(rec.fragments)},
        "findings": [asdict(f) for f in findings], "cameras": cams, "clips": clip_rows, "events": events,
        "ai_triage": ai_out, "ai": ai_full,
        "limitations": LIMITATIONS,
    }
    case["timeline"] = timeline.build(cl, events, md.get("logs", []), findings)
    led.append("results-written", findings=len(findings), clips=len(cl))
    ok, status = led.verify()
    case["custody"] = {"ok": ok, "status": status, "entries": led.entries()}
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str))
    log("::stage:: report")
    log("Rendering report ...")
    rep = report.render(case, case_dir, pdf=pdf)
    if "pdf" in rep:
        led.append("report-generated", pdf_sha256=hash_file(case_dir / rep["pdf"])["sha256"],
                   html_sha256=hash_file(case_dir / rep["html"])["sha256"])
    case["report"] = rep
    return case
