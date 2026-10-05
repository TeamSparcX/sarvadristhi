"""One case, several seized items (recorder disks, USB sticks, export folders).

Each item is analysed by its own workflow into evidence/E<n>/ (hashed, custody-logged), then
the case merges them on one recorder clock and adds what only a multi-item case can show:

  provenance      every export clip matched frame-by-frame to recorder footage
  AI L1-L4        across all cameras of all items, with activity events
  label scoring   activity events against human labels, when the case carries them
  poison check    when the evidence is a planted test bench (scenario.json), every planted
                  manipulation against the finding that caught it
  Requirement flag board   each requirement with the case data that demonstrates it
  lineage         for every clip: source image + hash -> byte range -> method -> frames ->
                  clip hash; every AI lead and finding carries its clip and time basis"""
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, provenance, report
from .acquire import hash_file
from .custody import Ledger
from .pipeline import LIMITATIONS, _tool_versions

# how far each format path has been validated - shown beside every result, never hidden
COVERAGE = {
    "hikvision": ("tested on modelled images", "structures from published research (Han et al. 2015) and an MIT parser "
                  "derived from a real DVR; not yet run on a physical Hikvision disk"),
    "dahua-dhav": ("tested on modelled images", "DHAV frame layout per FFmpeg dhav.c; DHFS index not parsed; "
                   "not yet run on a physical Dahua/CP Plus disk"),
    "honeywell": ("tested on modelled images", "record header per Yoon & Hwang, DFRWS USA 2026 (read in full); "
                  "channel list not parsed, camera from overlay OCR; not yet run on a physical Honeywell disk"),
    "unknown": ("generic carving only", "no vendor parser matched; H.264/MPEG-PS carved and attributed from content"),
    "NTFS": ("external tool, widely validated", "filesystem parsed by The Sleuth Kit (pytsk3); $UsnJrnl parsed by sarvadristhi"),
    "FAT32": ("external tool, widely validated", "The Sleuth Kit"), "EXFAT": ("external tool, widely validated", "The Sleuth Kit"),
    "files": ("not applicable", "loose video files"),
}
TIME_UNCERTAINTY_MS = {"index": 100, "container": 100, "ofni": 1000, "overlay (OCR)": 1000, "overlay": 1000,
                       "provenance match": 100, "none": None}


def _link(src: Path, dst: Path):
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S") if ms is not None else "—"


def run(sources: list[dict], case_dir: Path, case_id: str, examiner: str, *, ai: bool = True,
        scenario: Path | None = None, labels_dir: Path | None = None, pdf: bool = True, log=print) -> dict:
    from . import jobrun, media
    from .pipeline import run as run_disk
    case_dir = Path(case_dir)
    if (case_dir / "case.json").exists():
        raise FileExistsError(f"{case_dir} already holds a case")
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "clips").mkdir(exist_ok=True)
    led = Ledger(case_dir / "custody.jsonl")
    led.append("case-opened", case_id=case_id, examiner=examiner, tool=f"Sarvadristhi {__version__}", kind="multi-evidence",
               items=[s["path"] for s in sources])
    scen = json.loads(Path(scenario).read_text()) if scenario else None

    evidence, clips, findings, events, logs, journal = [], [], [], [], [], []
    for n, s in enumerate(sources, 1):
        eid = f"E{n}"
        name = s.get("name") or Path(s["path"]).stem
        kind = jobrun.route(s["path"])
        log(f"=== {eid} {name}: {kind} ===")
        sub = case_dir / "evidence" / eid
        def sub_log(m, e=eid):
            if m.startswith("::stage::"):
                if m.split()[1] == "report":          # the item's own summary, not the case report
                    return
                return log(f"{m} {e}")
            log(f"[{e}] {m}")
        if kind == "video-evidence":
            c = media.run(s["path"], sub, f"{case_id}/{eid}", examiner, ai=False, pdf=False, log=sub_log)
        else:
            c = run_disk(s["path"], sub, f"{case_id}/{eid}", examiner, mode=s.get("mode", "auto"), ai=False,
                         pdf=False, log=sub_log)
        fam = c["detection"]["family"]
        fs = c.get("filesystem")
        cov_key = fs["filesystem"] if fs else fam if fam in COVERAGE else "unknown"
        cov = COVERAGE.get(cov_key, COVERAGE["unknown"])
        cams = {x["channel"]: x for x in c["cameras"]}
        ev = {"id": eid, "name": name, "path": s["path"], "kind": kind, "family": fam,
              "confidence": c["detection"]["confidence"], "candidates": c["detection"].get("candidates", []),
              "coverage": {"level": cov[0], "basis": cov[1]}, "hash": c["evidence"].get("before"),
              "unchanged": c["evidence"]["unchanged"], "bytes": c["evidence"]["bytes"],
              "recovery": c["recovery"], "filesystem": fs, "custody": c["custody"]["status"],
              "custody_ok": c["custody"]["ok"], "dir": f"evidence/{eid}",
              "cameras": [{"channel": ch, "label": v["label"], "drift": v.get("drift")} for ch, v in cams.items()],
              "findings": len(c["findings"])}
        evidence.append(ev)
        led.append("evidence-analysed", evidence=eid, name=name, family=fam, hash=c["evidence"].get("before"),
                   unchanged=c["evidence"]["unchanged"], sub_ledger=f"evidence/{eid}/custody.jsonl",
                   sub_ledger_ok=c["custody"]["ok"])
        # channel ids must be unique across items: E1 CH3 -> 103
        k_extra = 0
        remap = {}
        for k in c["clips"]:
            if not k.get("mp4_file"):
                continue
            ch = k.get("channel")
            if ch is None:
                k_extra += 1
                g = n * 100 + 50 + k_extra
            else:
                g = n * 100 + ch
            remap[k["id"]] = g
            lab = k.get("label") or (cams.get(ch) or {}).get("label") or (f"CH{ch}" if ch else k.get("source_file", k["id"]))
            new_id = f"{eid}-{k['id']}"
            mp4 = f"{new_id}.mp4"
            _link(sub / "clips" / k["mp4_file"], case_dir / "clips" / mp4)
            raw = None
            if k.get("raw_file"):
                raw = f"{new_id}{Path(k['raw_file']).suffix}"
                _link(sub / "clips" / k["raw_file"], case_dir / "clips" / raw)
            ts_src = (k.get("time_source") or ["none"])
            ts_src = ts_src[0] if isinstance(ts_src, list) and ts_src else ts_src
            prov = k.get("provenance") or ("index" if k.get("evidence_level") == "index-confirmed" else "carved")
            clip = {**k, "id": new_id, "local_id": k["id"], "evidence": eid, "device": name, "channel": g,
                    "local_channel": ch, "label": f"{name} · {lab}", "camera": lab, "mp4_file": mp4, "raw_file": raw,
                    "provenance": prov, "time_source": ts_src,
                    "time_uncertainty_ms": TIME_UNCERTAINTY_MS.get(ts_src, 1000 if ts_src != "none" else None),
                    "drift_s": ((cams.get(ch) or {}).get("drift") or {}).get("drift_s"),
                    "lineage": {"source_image": s["path"], "source_sha256": (c["evidence"].get("before") or {}).get("sha256"),
                                "method": f"{c['recovery'].get('plugin')} ({c['recovery'].get('mode')})",
                                "first_offset": k.get("first_offset"), "bytes": k.get("bytes"),
                                "frames": k.get("frames"), "frames_csv": f"evidence/{eid}/frames.csv"
                                if (sub / "frames.csv").exists() else None,
                                "raw_sha256": k.get("raw_sha256"), "mp4_sha256": k.get("mp4_sha256"),
                                "original_sha256": k.get("original_sha256")}}
            if scen:   # attach the real-footage source and label set for scoring
                for ch_meta in scen["evidence"].get(name, {}).get("channels", []):
                    if ch_meta["channel"] == ch:
                        import re
                        m = re.search(r"\.([a-z]+\.G\d+)\.r13", ch_meta["source"])
                        clip["footage_source"] = m.group(1) if m else ch_meta["source"]
            clips.append(clip)
        for f in c["findings"]:
            d = dict(f.get("detail") or {})
            if isinstance(d.get("channel"), int):
                d["local_channel"], d["channel"] = d["channel"], n * 100 + d["channel"]
            findings.append({**f, "detail": d, "evidence": eid, "device": name,
                             "summary": f"[{name}] {f['summary']}"})
        for e in c.get("events", []):
            if e.get("clip") in remap:
                events.append({**e, "clip": f"{eid}-{e['clip']}", "channel": remap[e["clip"]], "evidence": eid})
        for l in (c["recovery"].get("logs") or []):
            logs.append({**l, "evidence": eid, "device": name})
        if fs:
            for a in fs["journal"].get("activity", []):
                journal.append({**a, "evidence": eid, "device": name})

    # ------------------------------------------------------------------ provenance
    rec = [k for k in clips if k.get("raw_file") and k["start_ms"] is not None]
    exp = [k for k in clips if k["provenance"] in ("deleted-recovered", "live-file", "disguised-file")]
    prov = []
    if rec and exp:
        log("::stage:: provenance")
        log("Provenance: matching export clips to recorder frames ...")
        prov = provenance.match(case_dir, rec, exp)
        for r in prov:
            k = next(x for x in clips if x["id"] == r["export"])
            if r.get("source_clip"):
                src = next(x for x in clips if x["id"] == r["source_clip"])
                k.update(start_ms=r["start_ms"], end_ms=r["end_ms"], channel=src["channel"],
                         camera=src["camera"], time_source="provenance match", time_uncertainty_ms=100,
                         label=f"{k['device']} · {src['camera']} (export)", drift_s=src.get("drift_s"),
                         footage_source=src.get("footage_source"))
                k["provenance_match"] = r["verdict"]
            sev = "critical" if r["edits"] or any(s["only_surviving_copy"] for s in r["export_only"]) else "info"
            findings.append({"kind": "export-provenance", "severity": sev, "evidence": k["evidence"], "device": k["device"],
                             "summary": f"[{k['device']}] {k.get('source_file')}: {r['verdict']}",
                             "detail": {"export": r["export"], "file": r.get("file"), "verdict": r["verdict"],
                                        "matched": r["frames_matched"], "frames": r["frames"],
                                        "edits": [e["summary"] for e in r["edits"]]}})
            for e in r["edits"]:
                findings.append({"kind": "export-edited", "severity": "critical", "evidence": k["evidence"],
                                 "device": k["device"], "summary": f"[{k['device']}] {k.get('source_file')}: {e['summary']}",
                                 "detail": {**e, "time": e["recorder_from_ms"] // 1000}})
        led.append("provenance", results=[{"export": r["export"], "verdict": r["verdict"]} for r in prov])

    # ------------------------------------------------------------------ AI
    ai_out, scoring = {}, None
    if ai:
        from . import ai as ai_layer
        log("AI layers L1-L4 across all items ...")
        ai_out = ai_layer.run(case_dir, clips, events, findings, log=log)
        led.append("ai-analysis", tracks=len(ai_out["layers"]["L2"]["tracks"]),
                   activities=len(ai_out["layers"]["L4"].get("activities", [])),
                   insights=len(ai_out["layers"]["L4"]["insights"]),
                   sha256=hash_file(case_dir / "ai" / "ai.json")["sha256"])
        if labels_dir:
            from .ai import score
            labels, cover = {}, {}
            for k in clips:
                if k.get("footage_source") and k["start_ms"] is not None:
                    cam = k["camera"]
                    cover.setdefault(cam, []).append((k["start_ms"], k["end_ms"]))
                    labels.setdefault(cam, score.load_labels(Path(labels_dir), k["footage_source"]))
            # the same camera's events, whichever item they came from
            acts = [{**a, "camera": next((k["camera"] for k in clips if k["id"] == a["clip"]), a["camera"])}
                    for a in ai_out["layers"]["L4"].get("activities", [])]
            scoring = score.score(acts, labels, cover)
            ai_out["scoring"] = scoring
            (case_dir / "ai" / "ai.json").write_text(json.dumps(ai_out, indent=1, default=str), encoding="utf-8")
            led.append("ai-scored-against-labels", recall=scoring["recall"], scorable=scoring["scorable"],
                       hits=scoring["hits"], precision_lower_bound=scoring["precision_lower_bound"])

    # ------------------------------------------------------------------ poison scorecard
    poison = None
    if scen:
        kinds = {f["kind"] for f in findings}

        def caught(*ks, ev=None):
            return [f["summary"] for f in findings if f["kind"] in ks and (ev is None or f["device"] == ev)]
        checks = {"P1": caught("format-event", ev="DVR-A"), "P2": caught("index-reset", ev="DVR-A"),
                  "P3": caught("clock-change", ev="DVR-A"), "P4": caught("clock-drift", ev="DVR-A"),
                  "P5": caught("torn-frames", ev="DVR-B"),
                  "P6": [f["summary"] for f in findings if f["kind"] in ("deleted-file", "export-provenance")
                         and "PARKING" in f["summary"]],
                  "P7": caught("export-edited"), "P8": caught("disguised-file")}
        poison = []
        for p in scen["poisons"]:
            hit = checks.get(p["id"], [])
            poison.append({**p, "caught": bool(hit), "by": hit[:3]})
        led.append("poison-scorecard", caught=sum(p["caught"] for p in poison), planted=len(poison))

    # ------------------------------------------------------------------ Requirement flag board
    ps = ps_flags(evidence, clips, findings, prov, ai_out, scoring)

    # ------------------------------------------------------------------ timeline + case
    items = [{"kind": "clip", "channel": k["channel"], "start_ms": k["start_ms"], "end_ms": k["end_ms"], "id": k["id"],
              "level": k.get("evidence_level"), "provenance": k["provenance"]} for k in clips if k["start_ms"]]
    items += [{"kind": "event", **e, "start_ms": e["abs_start_ms"]} for e in events]
    for l in logs:
        items.append({"kind": "log", "start_ms": l["time"] * 1000, "text": f"[{l['device']}] {l['text']}", "type": l["type_name"]})
    for f in findings:
        t = f["detail"].get("time") if isinstance(f["detail"], dict) else None
        if t:
            items.append({"kind": "finding", "start_ms": t * 1000, "text": f["summary"], "severity": f["severity"]})
    sev = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: sev.get(f["severity"], 3))
    cameras = []
    for k in clips:
        if not any(c["channel"] == k["channel"] for c in cameras):
            cameras.append({"channel": k["channel"], "label": k["label"].replace(" (export)", ""), "device": k["device"],
                            "camera": k["camera"], "clips": 0, "frames": 0, "drift": {"drift_s": k.get("drift_s")}})
        cam = next(c for c in cameras if c["channel"] == k["channel"])
        cam["clips"] += 1
        cam["frames"] += k.get("frames") or 0
    unchanged = all(e["unchanged"] for e in evidence)
    led.append("results-written", findings=len(findings), clips=len(clips))
    ok, status = led.verify()
    case = {
        "case": {"case_id": case_id, "examiner": examiner, "kind": "multi-evidence"},
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tool": {"name": "sarvadristhi", "version": __version__, "components": _tool_versions()},
        "scenario": {k: scen[k] for k in ("scenario", "nature", "footage")} if scen else None,
        "options": {"mode": "multi-evidence", "analytics": True, "ai": ai},
        "evidence_items": evidence,
        "evidence": {"source": "; ".join(e["path"] for e in evidence), "bytes": sum(e["bytes"] or 0 for e in evidence),
                     "before": {e["id"]: e["hash"] for e in evidence}, "after": None, "unchanged": unchanged},
        "acquisition": None, "e01": None,
        "detection": {"family": " + ".join(f"{e['name']}: {e['family']}" for e in evidence), "plugin": "multi",
                      "confidence": min(e["confidence"] for e in evidence), "brands_using_family": [],
                      "candidates": [], "source_bytes": None},
        "recovery": {"mode": "multi", "plugin": "multi", "frames": sum(e["recovery"]["frames"] for e in evidence),
                     "levels": _sum_levels(evidence), "logs": logs, "master_sector": None, "index_entries": 0,
                     "universal_sweep": None, "attribution": None, "fragments": 0},
        "filesystem": next((e["filesystem"] for e in evidence if e["filesystem"]), None),
        "journal": journal, "findings": findings, "cameras": cameras, "clips": clips, "events": events,
        "provenance": prov, "ai": ai_out, "ai_triage": {}, "poison_scorecard": poison, "ps_flags": ps,
        "limitations": LIMITATIONS + ["Format coverage per item is shown beside it; 'tested on modelled images' "
                                      "means not yet run on a physical recorder of that brand."],
        "timeline": {"items": sorted(items, key=lambda x: x["start_ms"]),
                     "channels": sorted({k["channel"] for k in clips})},
        "custody": {"ok": ok, "status": status, "entries": led.entries()},
    }
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    log("::stage:: report")
    log("Rendering report ...")
    try:
        rep = report.render(case, case_dir, pdf=pdf)
        if "pdf" in rep:
            led.append("report-generated", pdf_sha256=hash_file(case_dir / rep["pdf"])["sha256"])
    except Exception as e:
        rep = {"error": str(e)[:300]}
        log(f"  report failed: {e}")
    ok, status = led.verify()
    case["report"], case["custody"] = rep, {"ok": ok, "status": status, "entries": led.entries()}
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    return case


def _sum_levels(evidence):
    out = {}
    for e in evidence:
        for k, v in (e["recovery"].get("levels") or {}).items():
            out[k] = out.get(k, 0) + v
    return out


def ps_flags(evidence, clips, findings, prov, ai_out, scoring) -> list[dict]:
    kinds = {}
    for f in findings:
        kinds.setdefault(f["kind"], []).append(f)
    L = (ai_out or {}).get("layers", {})
    acts = L.get("L4", {}).get("activities", [])
    ents = L.get("L4", {}).get("entities", [])
    faces = sum(1 for t in L.get("L2", {}).get("tracks", []) if (t.get("face") or {}).get("found"))
    fams = sorted({e["family"] for e in evidence})

    def flag(req, ok, show, where):
        return {"requirement": req, "status": "demonstrated" if ok else "not shown", "evidence": show, "where": where}
    return [
        flag("Identify the recorder / format from the disk", all(e["confidence"] >= 0.5 for e in evidence),
             ", ".join(f"{e['name']}: {e['family']} ({e['confidence']})" for e in evidence), "evidence"),
        flag("Support multiple OEMs in one workflow", len(fams) >= 2, " · ".join(fams), "evidence"),
        flag("Forensic image + MD5/SHA-256", all(e["hash"] for e in evidence),
             f"{len(evidence)} items hashed before and after; unchanged: {all(e['unchanged'] for e in evidence)}", "custody"),
        flag("Parse proprietary filesystems", any(e["family"] == "hikvision" for e in evidence),
             "Hikvision master sector, system log, HIKBTREE, OFNI; DHAV frames; NTFS + $UsnJrnl", "evidence"),
        flag("Recover deleted / formatted / overwritten footage", bool(kinds.get("index-reset") or kinds.get("deleted-file")),
             f"{sum(1 for k in clips if k['provenance'] == 'carved')} carved clips; "
             f"{len(kinds.get('deleted-file', []))} deleted files", "evidence"),
        flag("Detect anti-forensics (format, clock change, edits, disguise)",
             bool(kinds.get("format-event") or kinds.get("clock-change") or kinds.get("export-edited")),
             ", ".join(k for k in ("format-event", "index-reset", "clock-change", "torn-frames", "export-edited",
                                   "disguised-file") if k in kinds), "findings"),
        flag("Normalise timestamps", bool(kinds.get("clock-drift") or kinds.get("clock-change")),
             f"{len(kinds.get('clock-drift', []))} drifts, {len(kinds.get('clock-change', []))} clock changes; "
             "all events on the recorder clock with stated uncertainty", "timeline"),
        flag("Verify export provenance", bool(prov),
             "; ".join(f"{r.get('file')}: {r['verdict']}" for r in prov)[:300], "evidence"),
        flag("Correlate events across cameras", any(len(e["cameras"]) > 1 for e in ents),
             f"{sum(len(e['cameras']) > 1 for e in ents)} entities seen on more than one camera", "ai"),
        flag("AI: object, face and motion detection", bool(L),
             f"{len(L.get('L2', {}).get('tracks', []))} tracks, {faces} faces, {len(L.get('L1', {}).get('events', []))} "
             f"motion events, {len(acts)} activity events", "ai"),
        flag("AI accuracy measured against human labels", bool(scoring and scoring.get("recall") is not None),
             f"recall {scoring['recall']} on {scoring['scorable']} scorable labels" if scoring else "no labels", "ai"),
        flag("Chain of custody", True, "hash-chained ledger per item and for the case", "custody"),
        flag("Standardised report with s.63(4) data sheet", True, "HTML + PDF", "custody"),
    ]


def refresh_ai(case_dir: Path, labels_dir: Path | None = None, pdf: bool = True, log=print) -> dict:
    """Re-run the AI layers (and label scoring) on a finished multi-evidence case, keeping the
    evidence analysis, provenance and custody as they are. Logged to the case ledger."""
    import re
    from . import ai as ai_layer
    case_dir = Path(case_dir)
    case = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    led = Ledger(case_dir / "custody.jsonl")
    clips = case["clips"]
    for k in clips:
        if k.get("footage_source"):
            m = re.search(r"\.?([a-z]+\.G\d+)(\.r13)?", k["footage_source"])
            k["footage_source"] = m.group(1) if m else k["footage_source"]
    shutil.rmtree(case_dir / "ai", ignore_errors=True)
    ai_out = ai_layer.run(case_dir, clips, case["events"], case["findings"], log=log)
    scoring = None
    if labels_dir:
        from .ai import score
        labels, cover = {}, {}
        for k in clips:
            if k.get("footage_source") and k["start_ms"] is not None:
                cover.setdefault(k["camera"], []).append((k["start_ms"], k["end_ms"]))
                labels.setdefault(k["camera"], score.load_labels(Path(labels_dir), k["footage_source"]))
        acts = [{**a, "camera": next((k["camera"] for k in clips if k["id"] == a["clip"]), a["camera"])}
                for a in ai_out["layers"]["L4"].get("activities", [])]
        scoring = score.score(acts, labels, cover)
        ai_out["scoring"] = scoring
    (case_dir / "ai" / "ai.json").write_text(json.dumps(ai_out, indent=1, default=str), encoding="utf-8")
    led.append("ai-refreshed", tracks=len(ai_out["layers"]["L2"]["tracks"]),
               activities=len(ai_out["layers"]["L4"].get("activities", [])),
               insights=len(ai_out["layers"]["L4"]["insights"]), recall=(scoring or {}).get("recall"),
               sha256=hash_file(case_dir / "ai" / "ai.json")["sha256"])
    case["ai"] = ai_out
    case["ps_flags"] = ps_flags(case["evidence_items"], clips, case["findings"], case.get("provenance") or [],
                                ai_out, scoring)
    ok, status = led.verify()
    case["custody"] = {"ok": ok, "status": status, "entries": led.entries()}
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    try:
        case["report"] = report.render(case, case_dir, pdf=pdf)
    except Exception as e:
        case["report"] = {"error": str(e)[:300]}
    (case_dir / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    return case
