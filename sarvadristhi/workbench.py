"""Read-only analyses the workbench computes from a finished case: format hypotheses, evidence
reconciliation, the capability matrix, case questions and video stills. Nothing here modifies a
case folder or an evidence image."""
import csv
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .capsule import resolve_source

HERE = Path(__file__).parent

# Brand strings are weak evidence: rebadging is common, so they are reported beside the structural
# checks, never instead of them. Structural signatures (HIKVISION@HANGZHOU, DHFS) are excluded.
BRANDS = {b"CP PLUS": "CP Plus", b"CPPLUS": "CP Plus", b"CP-PLUS": "CP Plus", b"HONEYWELL": "Honeywell",
          b"GODREJ": "Godrej", b"TP-LINK": "TP-Link", b"VIGI": "TP-Link VIGI", b"UNIVIEW": "Uniview",
          b"SATATYA": "Matrix", b"MATRIX COMSEC": "Matrix", b"DAHUA": "Dahua", b"HIKVISION ": "Hikvision"}
FAMILY_BRAND = {"hikvision": {"Hikvision"}, "dahua-dhav": {"Dahua", "CP Plus"}, "honeywell": {"Honeywell"}}


def _case(case_dir: Path) -> dict:
    return json.loads((Path(case_dir) / "case.json").read_text(encoding="utf-8"))


def _items(c: dict) -> list[dict]:
    if c.get("evidence_items"):
        return c["evidence_items"]
    return [{"id": "E1", "name": c["case"]["case_id"], "path": c["evidence"]["source"], "kind": c["case"].get("kind", "recorder-disk"),
             "family": c["detection"]["family"], "confidence": c["detection"]["confidence"],
             "candidates": c["detection"].get("candidates", []), "recovery": c["recovery"], "filesystem": c.get("filesystem"),
             "bytes": c["evidence"].get("bytes"), "dir": "", "cameras": [{"channel": k["channel"], "label": k["label"],
                                                                         "drift": k.get("drift")} for k in c.get("cameras", [])]}]


def _hms(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S") if ms is not None else "?"


# --------------------------------------------------------------------------- hypotheses
def brand_strings(path: Path, limit: int = 4 << 20) -> list[dict]:
    with open(path, "rb") as f:
        head = f.read(limit)
    up, out, seen = head.upper(), [], set()
    for sig, brand in BRANDS.items():
        i = up.find(sig)
        if i >= 0 and brand not in seen:
            seen.add(brand)
            ctx = head[max(0, i - 8):i + len(sig) + 24]
            out.append({"brand": brand, "offset": i, "text": ctx.decode("ascii", "replace").strip("\x00 ")[:60]})
    return out


def hypotheses(case_dir: Path) -> list[dict]:
    """Every format interpretation tested on each item, with the checks it passes and fails.
    The score is a structural-consistency score, not a probability."""
    c = _case(case_dir)
    out = []
    for it in _items(c):
        if it.get("kind") == "video-evidence":
            out.append({"evidence": it["id"], "name": it.get("name"), "kind": it["kind"], "state": "not a recorder disk",
                        "family": it.get("family"), "hypotheses": [], "brands": [],
                        "note": "Removable media or export files: analysed through the filesystem, not a DVR format."})
            continue
        rec = it.get("recovery") or {}
        p = resolve_source(it["path"])
        brands = brand_strings(p) if p else []
        hyps = []
        for cand in it.get("candidates") or []:
            ev, fam, won = cand.get("evidence") or {}, cand["family"], cand["plugin"] == rec.get("plugin")
            checks = []
            if cand["plugin"] == "hikvision":
                checks.append({"check": "master sector signature HIKVISION@HANGZHOU", "ok": bool(ev.get("signature")),
                               "detail": f"at 0x{ev.get('master_sector_offset', 0):x}" if ev.get("signature") else "not found"})
                if won:
                    ms = rec.get("master_sector") or {}
                    checks.append({"check": "HIKBTREE index (primary + backup)", "ok": bool(ms.get("btree1_offset")),
                                   "detail": f"{rec.get('index_entries') or 0} block entries in use"})
                    checks.append({"check": "system log (RATS records)", "ok": bool(rec.get("logs")),
                                   "detail": f"{len(rec.get('logs') or [])} records"})
                    lv = rec.get("levels") or {}
                    checks.append({"check": "frames placed by index or OFNI tables", "ok": bool(lv),
                                   "detail": ", ".join(f"{v:,} {k}" for k, v in lv.items())})
            elif cand["plugin"] == "dahua":
                n = ev.get("valid_frames_sampled") or 0
                checks.append({"check": "DHAV frame headers with matching footers", "ok": cand["confidence"] > 0.3,
                               "detail": f"{n:,} valid frames in {ev.get('windows_with_frames', 0)} sampled windows" if n else "none found"})
                checks.append({"check": "DHFS 4.1 disk index", "ok": None, "detail": "index not parsed by this version"})
                if won:
                    lv = rec.get("levels") or {}
                    checks.append({"check": "recorder timestamps in every frame", "ok": bool(lv),
                                   "detail": ", ".join(f"{v:,} {k}" for k, v in lv.items())})
            elif cand["plugin"] == "honeywell":
                n = ev.get("valid_records_sampled") or 0
                checks.append({"check": "20-byte record headers (type, 80 01 00, size, length, µs time)", "ok": cand["confidence"] > 0.3,
                               "detail": f"{n:,} chained records in {ev.get('windows_with_records', 0)} sampled windows" if n else "none found"})
                checks.append({"check": "Video Channel List", "ok": None, "detail": "not parsed: camera from overlay OCR"})
                if won:
                    lv = rec.get("levels") or {}
                    checks.append({"check": "recorder timestamp in every record", "ok": bool(lv),
                                   "detail": ", ".join(f"{v:,} {k}" for k, v in lv.items())})
            else:
                n = ev.get("ps_pack_headers_first_256MiB")
                if n is None:
                    n = next((v for v in ev.values() if isinstance(v, int)), None)
                checks.append({"check": "vendor-neutral video structure (MPEG-PS / H.264)", "ok": bool(n),
                               "detail": f"{n:,} MPEG-PS pack headers in the first 256 MiB" if n is not None else "sweep of unclaimed bytes"})
            for b in brands:
                if b["brand"] in FAMILY_BRAND.get(fam, set()):
                    checks.append({"check": f"brand string '{b['brand']}' (weak)", "ok": True, "detail": f"at 0x{b['offset']:x}"})
            hyps.append({"plugin": cand["plugin"], "family": fam, "score": cand["confidence"], "selected": won,
                         "role": "fallback" if cand["plugin"] == "universal" else "native", "checks": checks})
        hyps.sort(key=lambda h: -h["score"])
        native = [h for h in hyps if h["role"] == "native"]
        top = native[0] if native else None
        second = native[1]["score"] if len(native) > 1 else 0.0
        if not top or top["score"] < 0.5:
            state = "unresolved: universal path used"
        elif top["score"] - second < 0.15:
            state = "contested"
        else:
            state = "accepted"
        conflicts = [b for b in brands if top and top["score"] >= 0.5 and b["brand"] not in FAMILY_BRAND.get(top["family"], set())]
        out.append({"evidence": it["id"], "name": it.get("name"), "kind": it.get("kind"), "state": state,
                    "family": it.get("family"), "hypotheses": hyps, "brands": brands,
                    "label_conflicts": conflicts,
                    "note": ("Label disagrees with structure: " + ", ".join(f"'{b['brand']}' string at 0x{b['offset']:x}" for b in conflicts)
                             + f", but the bytes are {top['family']}. Brand is context; structure decides.") if conflicts else ""})
    return out


# --------------------------------------------------------------------------- reconciliation
def _frames(case_dir: Path, it: dict) -> list[dict]:
    p = Path(case_dir) / (it.get("dir") or "") / "frames.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)]


def reconcile(case_dir: Path) -> dict:
    """Compare independent sources per camera: recorder index, physical frames on disk, recorder
    log, burned-in overlay clock and exported copies. A difference is reported as a relation,
    never automatically as a contradiction."""
    case_dir = Path(case_dir)
    c = _case(case_dir)
    clips = c["clips"]
    rows = []
    fmt_events = []
    for it in _items(c):
        if it.get("kind") == "video-evidence":
            continue
        rec = it.get("recovery") or {}
        for lg in rec.get("logs") or []:
            if "format" in lg.get("text", "").lower() or "initiali" in lg.get("text", "").lower():
                fmt_events.append({"evidence": it["id"], "ms": lg["time"] * 1000, "text": lg["text"], "offset": lg.get("offset")})
        by_ch = defaultdict(list)
        for r in _frames(case_dir, it):
            if r["time_ms"]:
                by_ch[int(r["channel"]) if r["channel"] else None].append(r)
        cams = {x["channel"]: x for x in it.get("cameras") or []}
        for ch, fr in sorted(by_ch.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
            label = (cams.get(ch) or {}).get("label") or f"CH{ch}"
            idx = [int(r["time_ms"]) for r in fr if r["evidence_level"] == "index-confirmed"]
            phys = [int(r["time_ms"]) for r in fr]
            carved = [int(r["time_ms"]) for r in fr if r["evidence_level"] != "index-confirmed"]
            cells = {}
            # index vs physical
            if not idx:
                cells["index"] = {"rel": "cannot compare" if rec.get("plugin") != "hikvision" else "index empty",
                                  "text": "no live index entries for this camera" if rec.get("plugin") == "hikvision"
                                  else "this format's index is not parsed", "tone": "mut"}
            else:
                cells["index"] = {"rel": "consistent", "text": f"index lists {_hms(min(idx))}–{_hms(max(idx))}", "tone": "ok"}
            if carved and idx:
                before = [t for t in carved if t < min(idx)]
                cells["disk"] = {"rel": "extends beyond", "text": f"{len(carved):,} frames the index does not list"
                                 + (f", {len(before):,} before {_hms(min(idx))}" if before else ""), "tone": "warn"}
            elif carved:
                cells["disk"] = {"rel": "unindexed only", "text": f"{len(carved):,} carved frames {_hms(min(carved))}–{_hms(max(carved))}",
                                 "tone": "warn"}
            else:
                cells["disk"] = {"rel": "consistent", "text": "every frame on disk is in the index", "tone": "ok"}
            # log
            ev = [e for e in fmt_events if e["evidence"] == it["id"]]
            if ev:
                e = ev[0]
                after = [t for t in idx if t >= e["ms"]]
                pre = [t for t in phys if t < e["ms"]]
                cells["log"] = {"rel": "discrepant" if pre and not (set(pre) & set(idx)) else "consistent",
                                "text": f"log: '{e['text']}' at {_hms(e['ms'])}; " + (f"{len(pre):,} frames predate it, none indexed" if pre else "no older frames"),
                                "tone": "crit" if pre else "ok"}
            elif rec.get("logs") is not None and rec.get("logs"):
                cells["log"] = {"rel": "consistent", "text": f"{len(rec['logs'])} log records, no format event", "tone": "ok"}
            else:
                cells["log"] = {"rel": "cannot compare", "text": "no recorder log for this format", "tone": "mut"}
            # overlay clock
            step = next((f for f in c["findings"] if f["kind"] == "clock-change" and f.get("evidence", it["id"]) == it["id"]
                         and (f.get("detail") or {}).get("local_channel", (f.get("detail") or {}).get("channel")) == ch), None)
            drift = ((cams.get(ch) or {}).get("drift") or {}).get("drift_s")
            if step:
                st = (step["detail"].get("steps") or [{}])[0]
                cells["overlay"] = {"rel": "discrepant", "text": f"clock steps {st.get('before_s', 0):+g} s → {st.get('after_s', 0):+g} s", "tone": "crit"}
            elif drift:
                cells["overlay"] = {"rel": "offset", "text": f"overlay clock {drift:+g} s from recorder", "tone": "warn"}
            elif drift == 0:
                cells["overlay"] = {"rel": "consistent", "text": "overlay agrees with recorder (±1 s)", "tone": "ok"}
            else:
                cells["overlay"] = {"rel": "cannot compare", "text": "overlay not read", "tone": "mut"}
            # exports
            gch = int(it["id"][1:]) * 100 + ch if c.get("evidence_items") and ch is not None else ch
            prov = [r for r in c.get("provenance") or [] if (next((k for k in clips if k["id"] == r.get("source_clip")), {}) or {}).get("channel") == gch]
            if prov:
                bad = [r for r in prov if r.get("edits")]
                only = [r for r in prov if any(s.get("only_surviving_copy") for s in r.get("export_only", []))]
                tone = "crit" if bad else "warn" if only else "ok"
                txt = "; ".join(r["verdict"] for r in prov)[:220]
                cells["export"] = {"rel": "discrepant" if bad else "extends beyond" if only else "consistent", "text": txt, "tone": tone}
            else:
                cells["export"] = {"rel": "cannot compare", "text": "no export of this camera", "tone": "mut"}
            rows.append({"evidence": it["id"], "device": it.get("name"), "channel": ch, "camera": label,
                         "frames": len(phys), "cells": cells})
    return {"columns": [["index", "Recorder index"], ["disk", "Frames on disk"], ["log", "Recorder log"],
                        ["overlay", "Overlay clock"], ["export", "Exported copies"]],
            "relations": ["consistent", "extends beyond", "offset", "discrepant", "cannot compare"], "rows": rows}


# --------------------------------------------------------------------------- capability matrix
def capabilities() -> dict:
    return json.loads((HERE / "capabilities.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- ask the case
COLOURS = ["black", "white", "grey", "gray", "red", "blue", "green", "yellow", "orange", "brown", "purple", "pink", "silver"]
VEHICLE = {"car", "cars", "vehicle", "vehicles", "truck", "trucks", "van", "suv", "auto"}
PERSON = {"person", "persons", "people", "man", "men", "woman", "women", "someone", "pedestrian", "pedestrians", "individual"}
ACTS = [(r"\b(get|gets|got|getting) (in|into)\b|\bboard", ["enter_vehicle"]),
        (r"\b(get|gets|got|getting) out\b|\balight", ["exit_vehicle"]),
        (r"\bent(er|ers|ered|ering)\b|\barriv|\bcome in|\bcame in|\bwalk(s|ed)? in", ["person_enters", "enter_vehicle"]),
        (r"\bexit|\bleav|\bleft\b|\bwalk(s|ed)? out|\bdepart", ["person_exits", "exit_vehicle"]),
        (r"\bstart|\bdrive off|\bdrove off|\bpull(s|ed)? out", ["vehicle_starts"]),
        (r"\bstop|\bpark(s|ed)?\b", ["vehicle_stops"]),
        (r"\bmov|\bdriv|\breversing|\breverse|\bturn", ["vehicle_moving"])]
FINDING_WORDS = {"delet": ["deleted-file"], "edit": ["export-edited"], "remov": ["export-edited"], "cut": ["export-edited"],
                 "tamper": ["export-edited", "clock-change", "format-event", "disguised-file"], "format": ["format-event", "index-reset"],
                 "wip": ["format-event"], "clock": ["clock-change", "clock-drift"], "drift": ["clock-drift"], "time chang": ["clock-change"],
                 "hid": ["disguised-file"], "disguis": ["disguised-file"], "overwrit": ["torn-frames", "index-reset"],
                 "gap": ["recording-gap"], "missing": ["recording-gap", "export-edited"], "copy": ["export-provenance"], "export": ["export-provenance", "export-edited"]}
STOP = {"camera", "cam", "the", "and", "when", "what", "show", "find", "with", "from", "after", "before", "between", "near",
        "were", "was", "did", "does", "any", "all", "time", "there", "this", "that", "into", "onto", "where", "which", "door"}


def _parse_time(tok: str, day_ms: int) -> int | None:
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*(am|pm)?", tok.strip())
    if not m:
        return None
    h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
    if m[4] == "pm" and h < 12:
        h += 12
    if m[4] == "am" and h == 12:
        h = 0
    return day_ms + ((h * 60 + mi) * 60 + s) * 1000


def ask(case_dir: Path, question: str) -> dict:
    """Answer a question only from facts stored in the case, with a citation for every item.
    No free-text generation: if nothing in the case matches, the answer says so."""
    c = _case(case_dir)
    q = question.strip()
    ql = q.lower()
    layers = (c.get("ai") or {}).get("layers") or {}
    if not layers:
        a = Path(case_dir) / "ai" / "ai.json"
        layers = json.loads(a.read_text(encoding="utf-8")).get("layers", {}) if a.exists() else {}
    acts = (layers.get("L4") or {}).get("activities") or []
    tracks = {t["id"]: t for t in (layers.get("L2") or {}).get("tracks") or []}
    t0 = min((k["start_ms"] for k in c["clips"] if k.get("start_ms")), default=0)
    day = t0 - t0 % 86400000
    parsed = {"cameras": [], "times": [], "subject": None, "colours": [], "activities": [], "findings": []}
    if re.search(r"\bwho\b|\bname of\b|\bidentif|\bwhose\b", ql):
        return {"question": q, "refused": True, "answer": "Sarvadristhi does not identify people. It can find when and where a person, "
                "vehicle or activity appears, and cite the frames, for an examiner to review.", "citations": [], "parsed": parsed}
    # time window
    times = [t for t in (_parse_time(x, day) for x in re.findall(r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?\s*(?:am|pm)?", ql)) if t]
    lo, hi = None, None
    if len(times) >= 2 and re.search(r"between|from|to|and", ql):
        lo, hi = min(times[:2]), max(times[:2]) + 59999 * (times[1] % 60000 == 0)
    elif times and "after" in ql:
        lo = times[0]
    elif times and "before" in ql:
        hi = times[0]
    elif times:
        lo, hi = times[0] - 30000, times[0] + 90000
    parsed["times"] = [_hms(x) for x in (lo, hi) if x]
    # cameras
    cams = sorted({k["label"] for k in c["clips"] if k.get("label")})
    words = [w for w in re.findall(r"[a-z0-9-]+", ql) if w not in STOP and len(w) > 2]
    m = re.search(r"\b(?:cam(?:era)?)[\s-]*0?(\d{1,2})\b", ql)
    picked = []
    for lab in cams:
        ll = lab.lower()
        if m and re.search(rf"cam-0?{int(m[1])}\b", ll):
            picked.append(lab)
        elif any(w in ll and w not in ("bus",) or (w == "bus" and "lobby" in ql and "bus" in ll) for w in words if w not in ("dvr-a", "dvr-b")):
            if not any(w in ll for w in VEHICLE | PERSON):
                picked.append(lab)
    dev = [d for d in ("dvr-a", "dvr-b", "usb") if d in ql]
    if dev and not picked:
        picked = [lab for lab in cams if any(lab.lower().startswith(d) for d in dev)]
    parsed["cameras"] = picked
    # subject, colour, activity
    toks = set(re.findall(r"[a-z]+", ql))
    if toks & PERSON:
        parsed["subject"] = "person"
    elif toks & VEHICLE or ("bus" in toks and "lobby" not in ql):
        parsed["subject"] = "vehicle"
    parsed["colours"] = [("grey" if x == "gray" else x) for x in COLOURS if re.search(rf"\b{x}\b", ql)]
    for rx, types in ACTS:
        if re.search(rx, ql):
            parsed["activities"] += [t for t in types if t not in parsed["activities"]]
    for w, kinds in FINDING_WORDS.items():
        if w in ql:
            parsed["findings"] += [k for k in kinds if k not in parsed["findings"]]
    if parsed["subject"] == "person":
        parsed["activities"] = [a for a in parsed["activities"] if not a.startswith("vehicle")]
    if parsed["subject"] == "vehicle":
        parsed["activities"] = [a for a in parsed["activities"] if a.startswith("vehicle") or a.endswith("vehicle")]

    def in_win(ms):
        return (lo is None or ms >= lo) and (hi is None or ms <= hi)

    def cam_ok(lab):
        return not picked or lab in picked

    cites = []
    if parsed["findings"]:
        for f in c["findings"]:
            if f["kind"] in parsed["findings"]:
                cites.append({"type": "finding", "id": f["kind"], "camera": f.get("device") or f.get("evidence") or "",
                              "time": "", "text": f["summary"], "severity": f["severity"]})
    vcls = {"car", "truck", "bus", "motorcycle"}
    if parsed["activities"] or (parsed["subject"] and not parsed["findings"]) or parsed["colours"]:
        types = parsed["activities"] or ([a for a in {x["type"] for x in acts} if (parsed["subject"] == "vehicle") == (a.startswith("vehicle") or "vehicle" in a)]
                                         if parsed["subject"] else [])
        for a in acts:
            if types and a["type"] not in types:
                continue
            ms = a["from_ms"] + 1000
            if not in_win(ms) or not cam_ok(a["camera"]):
                continue
            t = tracks.get(a.get("track")) or {}
            if parsed["colours"] and not ({t.get("upper_colour"), t.get("lower_colour")} & set(parsed["colours"])):
                continue
            cites.append({"type": "activity", "id": a["id"], "activity": a["type"], "camera": a["camera"], "ms": ms,
                          "time": _hms(ms), "clip": a["clip"], "track": a.get("track"), "thumb": t.get("thumb"),
                          "crop": t.get("crop"), "confidence": a["confidence"], "colour": t.get("upper_colour"),
                          "text": a["why"]})
        if not types and parsed["colours"]:
            for t in tracks.values():
                want_v = parsed["subject"] == "vehicle"
                if parsed["subject"] and (t["cls"] in vcls) != want_v:
                    continue
                if not ({t.get("upper_colour"), t.get("lower_colour")} & set(parsed["colours"])):
                    continue
                if not in_win(t["abs_best_ms"]) or not cam_ok(t.get("channel_name", "")):
                    continue
                cites.append({"type": "track", "id": t["id"], "activity": t["cls"], "camera": t.get("channel_name"),
                              "ms": t["abs_best_ms"], "time": _hms(t["abs_best_ms"]), "clip": t["clip"], "track": t["id"],
                              "thumb": t.get("thumb"), "crop": t.get("crop"), "confidence": t.get("max_conf"),
                              "colour": t.get("upper_colour"), "text": f"{t['cls']} track, {t['duration_s']:.0f} s on screen"})
    understood = any(parsed[k] for k in ("cameras", "times", "subject", "colours", "activities", "findings"))
    if not understood:
        return {"question": q, "refused": True, "parsed": parsed, "citations": [],
                "answer": "I could not match this question to anything sarvadristhi records. Ask about a camera, a time, people, "
                          "vehicles, a colour, an activity (enters, exits, starts, stops, gets into a vehicle) or a finding "
                          "(deleted, edited, formatted, clock change, disguised file)."}
    timed = sorted((x for x in cites if x.get("ms")), key=lambda x: x["ms"])
    others = [x for x in cites if not x.get("ms")]
    cites = others + timed
    if not cites:
        return {"question": q, "refused": True, "parsed": parsed, "citations": [],
                "answer": "No evidence in this case matches: " + _describe(parsed) + ". sarvadristhi does not guess; widen the time window "
                          "or remove a condition."}
    parts = []
    if others:
        parts.append(f"{len(others)} finding{'s' if len(others) > 1 else ''}: " + "; ".join(x["text"] for x in others[:3]))
    if timed:
        by_cam = defaultdict(int)
        for x in timed:
            by_cam[x["camera"]] += 1
        kinds = sorted({x["activity"].replace("_", " ") for x in timed})
        parts.append(f"{len(timed)} matching {'event' if timed[0]['type'] == 'activity' else 'track'}{'s' if len(timed) > 1 else ''} "
                     f"({', '.join(kinds)}) between {timed[0]['time']} and {timed[-1]['time']} recorder time, on "
                     + ", ".join(f"{cam} ({n})" for cam, n in sorted(by_cam.items(), key=lambda kv: -kv[1])[:4]))
    return {"question": q, "refused": False, "parsed": parsed, "answer": ". ".join(parts) + ".", "citations": cites[:40],
            "total": len(cites), "basis": "Built only from the case's stored detections, activity rules and findings. "
                                          "AI-derived items are leads until an examiner accepts them."}


def _describe(p: dict) -> str:
    bits = []
    if p["colours"]:
        bits.append("/".join(p["colours"]))
    if p["subject"] and not p["activities"]:
        bits.append(p["subject"])
    if p["activities"]:
        bits.append(" or ".join(a.replace("_", " ") for a in p["activities"]))
    if p["findings"]:
        bits.append("findings: " + ", ".join(p["findings"]))
    if p["cameras"]:
        bits.append("on " + ", ".join(p["cameras"]))
    if p["times"]:
        bits.append("time " + "–".join(p["times"]))
    return " ".join(bits) or "the question"


# --------------------------------------------------------------------------- stills
def still(cases_root: Path, case: str, clip_file: str, at: float = 1.0) -> Path | None:
    """One JPEG frame from an exported clip, cached outside the case folder (cases/.cache)."""
    src = Path(cases_root) / case / "clips" / clip_file
    if not src.is_file():
        return None
    key = hashlib.sha1(f"{case}/{clip_file}/{at}".encode()).hexdigest()[:16]
    out = Path(cases_root) / ".cache" / f"{key}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(src), "-frames:v", "1", "-vf",
                        "scale='min(1280,iw)':-2", "-q:v", "3", str(out)], capture_output=True)
    return out if out.exists() else None
