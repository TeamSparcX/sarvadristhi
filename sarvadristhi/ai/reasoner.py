"""Layer 4 - context and decisions: who/what appears where and when, across cameras.

Design rule: rules decide, the language model only writes. Every conclusion here comes from
an explicit, re-runnable rule with its inputs and weights shown. The LLM narrative at the end
is constrained to the rule outputs, must cite them, and is machine-checked; sentences that
fail the check are flagged and kept out of the report.

  4a  time normalisation   every track placed on the recorder clock (clip start + offset),
                           with the camera's measured overlay drift noted
  4b  cross-camera links   pairwise score = appearance + time + exit/entry geometry + VLM
                           attributes + face; one-to-one assignment; union into entities
  4c  context rules        R1..R8 below, each producing an insight with citations
  4d  narrative            local LLM summary, citations verified sentence by sentence"""
import json
import re
from datetime import datetime, timezone

import cv2
import numpy as np

W_APPEAR, W_TIME, W_GEOM, W_VLM, W_FACE = 0.40, 0.25, 0.15, 0.10, 0.10
LINK_THRESHOLD = 0.60
MIN_APPEARANCE = 0.45      # never link on time alone: appearance must agree
MAX_GAP_S = 120


def hhmmss(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S") if ms is not None else "?"


def _hist_sim(a, b) -> float:
    return max(0.0, float(cv2.compareHist(a.astype(np.float32), b.astype(np.float32), cv2.HISTCMP_CORREL)))


def _edge(box, size) -> str | None:
    w, h = size
    cx = (box[0] + box[2]) / 2
    if cx < w * 0.2:
        return "left"
    if cx > w * 0.8:
        return "right"
    return None


def link_score(a: dict, b: dict, vlm_by_track: dict) -> dict:
    """Score how likely tracks a (earlier) and b are the same object on two cameras."""
    f = []
    sa, sb = a.get("_sig"), b.get("_sig")
    if sa and sb:
        up, lo = _hist_sim(sa["upper"], sb["upper"]), _hist_sim(sa["lower"], sb["lower"])
        ap = 0.6 * up + 0.4 * lo
        f.append({"factor": "appearance (colour histograms)", "value": round(ap, 2), "weight": W_APPEAR,
                  "why": f"upper {up:.2f}, lower {lo:.2f}; colours {a['upper_colour']}/{a['lower_colour']} vs "
                         f"{b['upper_colour']}/{b['lower_colour']}"})
    else:
        f.append({"factor": "appearance (colour histograms)", "value": 0.0, "weight": W_APPEAR, "why": "no usable crop"})
    gap = (b["abs_start_ms"] - a["abs_end_ms"]) / 1000
    overlap = gap < 0
    tv = 1.0 if overlap else max(0.0, 1 - gap / MAX_GAP_S)
    f.append({"factor": "time", "value": round(tv, 2), "weight": W_TIME,
              "why": "views overlap in time" if overlap else f"{gap:.1f} s between last sighting and first sighting"})
    ea, eb = _edge(a["exit_box"], a["frame_size"]), _edge(b["entry_box"], b["frame_size"])
    gv = 1.0 if ea and eb else 0.5 if ea or eb else 0.0
    f.append({"factor": "exit/entry geometry", "value": gv, "weight": W_GEOM,
              "why": f"leaves {a['channel_name']} at {ea or 'centre'}, enters {b['channel_name']} at {eb or 'centre'}"})
    va, vb = vlm_by_track.get(a["id"]), vlm_by_track.get(b["id"])
    if va and vb and va.get("ok") and vb.get("ok"):
        x, y = va["answer"], vb["answer"]
        same = [k for k in ("upper_clothing_colour", "lower_clothing_colour")
                if x.get(k) and y.get(k) and "unclear" not in x[k].lower() and x[k].lower() == y[k].lower()]
        vv = len(same) / 2
        f.append({"factor": "VLM attributes", "value": vv, "weight": W_VLM,
                  "why": f"VLM: {x.get('upper_clothing_colour')}/{x.get('lower_clothing_colour')} vs "
                         f"{y.get('upper_clothing_colour')}/{y.get('lower_clothing_colour')}"})
    else:
        f.append({"factor": "VLM attributes", "value": 0.5, "weight": W_VLM, "why": "not described by VLM: neutral"})
    if a.get("_face") is not None and b.get("_face") is not None:
        cos = float(np.dot(a["_face"], b["_face"]) / (np.linalg.norm(a["_face"]) * np.linalg.norm(b["_face"]) + 1e-9))
        fv = 1.0 if cos >= 0.363 else max(0.0, cos / 0.363 * 0.5)
        f.append({"factor": "face (SFace cosine)", "value": round(fv, 2), "weight": W_FACE,
                  "why": f"cosine {cos:.3f} (SFace same-person threshold 0.363)"})
    else:
        f.append({"factor": "face (SFace cosine)", "value": 0.5, "weight": W_FACE, "why": "no face on both: neutral"})
    score = sum(x["value"] * x["weight"] for x in f)
    return {"a": a["id"], "b": b["id"], "score": round(score, 3), "factors": f, "gap_s": round(gap, 1)}


def correlate(tracks: list[dict], vlm_by_track: dict) -> tuple[list, list, list]:
    """Return (all candidate pairs scored, accepted links, entities)."""
    movers = [t for t in tracks if t["cls"] in ("person", "car", "truck", "bus", "motorcycle", "bicycle")
              and t["observations"] >= 3]
    cands = []
    for a in movers:
        for b in movers:
            if a["channel"] == b["channel"] or a["cls"] != b["cls"]:
                continue
            if b["abs_start_ms"] < a["abs_start_ms"]:
                continue
            if (b["abs_start_ms"] - a["abs_end_ms"]) / 1000 > MAX_GAP_S:
                continue
            cands.append(link_score(a, b, vlm_by_track))
    cands.sort(key=lambda c: -c["score"])
    by_id = {t["id"]: t for t in tracks}
    parent = {t["id"]: t["id"] for t in tracks}
    members = {t["id"]: [t["id"]] for t in tracks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def clash(ga, gb) -> str | None:
        """One object cannot be two tracks on the same camera at the same time."""
        for x in members[ga]:
            for y in members[gb]:
                p, q = by_id[x], by_id[y]
                if p["channel"] == q["channel"] and p["abs_start_ms"] < q["abs_end_ms"] and q["abs_start_ms"] < p["abs_end_ms"]:
                    return f"{x} and {y} overlap in time on {p['channel_name']}"
        return None

    used_a, used_b, links = set(), set(), []
    for c in cands:                       # greedy, strongest first; each track continues at most once
        if c["score"] < LINK_THRESHOLD:
            c["decision"] = "below threshold"
        elif c["factors"][0]["value"] < MIN_APPEARANCE:
            c["decision"] = "appearance disagrees"
        elif c["a"] in used_a or c["b"] in used_b:
            c["decision"] = "a stronger link already uses one of these tracks"
        else:
            ga, gb = find(c["a"]), find(c["b"])
            why = clash(ga, gb) if ga != gb else None
            if why:
                c["decision"] = f"impossible: {why}"
                continue
            c["decision"] = "linked"
            links.append({**c, "accepted": True})
            used_a.add(c["a"])
            used_b.add(c["b"])
            if ga != gb:
                parent[ga] = gb
                members[gb] += members.pop(ga)
    groups = {}
    for t in movers:
        groups.setdefault(find(t["id"]), []).append(t)
    entities = []
    for k, (root, ts) in enumerate(sorted(groups.items(), key=lambda g: min(t["abs_start_ms"] for t in g[1])), 1):
        ts.sort(key=lambda t: t["abs_start_ms"])
        prefix = "P" if ts[0]["cls"] == "person" else "V"
        entities.append({"id": f"{prefix}{k}", "cls": ts[0]["cls"], "tracks": [t["id"] for t in ts],
                         "cameras": sorted({t["channel_name"] for t in ts}),
                         "first_ms": ts[0]["abs_start_ms"], "last_ms": max(t["abs_end_ms"] for t in ts),
                         "path": [{"camera": t["channel_name"], "from": hhmmss(t["abs_start_ms"]),
                                   "to": hhmmss(t["abs_end_ms"]), "track": t["id"]} for t in ts],
                         "colours": f"{ts[0].get('upper_colour') or '?'} top / {ts[0].get('lower_colour') or '?'} bottom"})
    for l in links:
        l["entity"] = next(e["id"] for e in entities if l["a"] in e["tracks"])
    return cands[:60], links, entities


def rules(tracks, entities, clips, findings, vlm_by_track, checks) -> list[dict]:
    """Context rules. Each insight: rule, title, statement, confidence, reasons, citations."""
    out = []
    by_id = {t["id"]: t for t in tracks}
    clip_by = {c["id"]: c for c in clips}

    def cite(t):
        c = clip_by[t["clip"]]
        return {"clip": t["clip"], "camera": t["channel_name"], "t_s": t["best_t"], "time": hhmmss(t["abs_best_ms"]),
                "track": t["id"], "sha256": (c.get("mp4_sha256") or "")[:16], "thumb": t["thumb"]}

    def add(rule, sev, title, statement, conf, reasons, cites):
        out.append({"id": f"I{len(out) + 1}", "rule": rule, "severity": sev, "title": title, "statement": statement,
                    "confidence": round(conf, 2), "reasons": reasons, "citations": cites})

    # R0 overview per camera: the plain counts every summary needs
    for cam in sorted({t["channel_name"] for t in tracks}):
        ts = [t for t in tracks if t["channel_name"] == cam]
        kinds = {}
        for t in ts:
            kinds[t["cls"]] = kinds.get(t["cls"], 0) + 1
        ents = [e for e in entities if cam in e["cameras"]]
        add("R0 overview", "info", f"{cam}: {len(ents)} distinct moving objects",
            f"{cam} recorded from {hhmmss(min(t['abs_start_ms'] for t in ts))} to "
            f"{hhmmss(max(t['abs_end_ms'] for t in ts))}; {len(ents)} distinct moving objects "
            f"({', '.join(f'{v} {k} track(s)' for k, v in sorted(kinds.items()))}).",
            1.0, ["counts of layer-2 tracks and layer-4 entities"],
            [cite(max(ts, key=lambda t: t["observations"]))])
    # R1 movement across cameras (strongest first, at most 6)
    multi = sorted([e for e in entities if len(e["cameras"]) >= 2], key=lambda e: -len(e["tracks"]))[:6]
    for e in multi:
        if True:
            ts = [by_id[t] for t in e["tracks"]]
            path = " -> ".join(f"{p['camera']} {p['from']}-{p['to']}" for p in e["path"])
            add("R1 cross-camera movement", "lead", f"{e['id']} ({e['cls']}) seen on {len(e['cameras'])} cameras",
                f"{e['id']}, a {e['cls']} in {e['colours']}, moves {path}.",
                min(0.95, 0.5 + 0.1 * len(ts)), [f"tracks linked by layer-4b scores >= {LINK_THRESHOLD}"],
                [cite(t) for t in ts])
    # R2 presence in deleted / carved footage
    for c in clips:
        if c.get("provenance") in ("deleted-recovered", "carved"):
            ts = [t for t in tracks if t["clip"] == c["id"] and t["cls"] == "person"]
            if ts:
                add("R2 people in deleted footage", "critical",
                    f"{len(ts)} person track(s) in footage that had been deleted",
                    f"Clip {c['id']} ({c.get('label') or c.get('channel')}) was recovered from "
                    f"{'a deleted file' if c['provenance'] == 'deleted-recovered' else 'unindexed disk space'} "
                    f"and shows {len(ts)} person track(s).",
                    0.9, [f"clip provenance: {c['provenance']}", c.get("provenance_detail", "")],
                    [cite(t) for t in ts[:6]])
    # R3 activity at the edge of missing footage
    for f in findings:
        if f.get("kind") == "recording-gap":
            d = f["detail"]
            near = [t for t in tracks if t["channel"] == d.get("channel") and t["cls"] == "person" and
                    (abs(t["abs_end_ms"] - d["from_ms"]) < 10000 or abs(t["abs_start_ms"] - d["to_ms"]) < 10000)]
            if near:
                add("R3 activity at a recording gap", "warning", "Person present just before/after missing footage",
                    f"{len(near)} person track(s) within 10 s of a {d['seconds']:.0f} s gap on camera {d['channel']}.",
                    0.7, ["recording gaps are where deletion or tampering is most likely"], [cite(t) for t in near])
    # R4 dwell / loitering (4 longest)
    for t in sorted(tracks, key=lambda t: -t["duration_s"])[:4]:
        if t["cls"] == "person" and t["duration_s"] >= 20:
            add("R4 dwell", "lead", f"{t['id']} stays {t['duration_s']:.0f} s in view",
                f"A person ({t.get('upper_colour') or '?'} top) remains in {t['channel_name']} view for "
                f"{t['duration_s']:.0f} s.", min(0.9, t["mean_conf"]), ["duration threshold 20 s"], [cite(t)])
    # R5 group
    by_cam_time = {}
    for t in tracks:
        if t["cls"] == "person":
            for ts_, _ in t["path"]:
                by_cam_time.setdefault((t["channel_name"], ts_), set()).add(t["id"])
    peak = max(by_cam_time.items(), key=lambda kv: len(kv[1]), default=None)
    if peak and len(peak[1]) >= 3:
        (cam, ts_), ids = peak
        add("R5 group", "lead", f"{len(ids)} people together on {cam}",
            f"Up to {len(ids)} people are visible at once on {cam}.", 0.75,
            ["simultaneous person tracks >= 3"], [cite(by_id[i]) for i in sorted(ids)][:6])
    # R6 objects of interest (knife first, then the strongest five bags)
    ooi = sorted([t for t in tracks if t["cls"] in ("knife", "suitcase", "backpack", "handbag") and t["mean_conf"] >= 0.5],
                 key=lambda t: (t["cls"] != "knife", -t["observations"] * t["mean_conf"]))[:5]
    for t in ooi:
        if True:
            add("R6 object of interest", "warning" if t["cls"] == "knife" else "lead", f"{t['cls']} detected",
                f"A {t['cls']} is detected on {t['channel_name']} ({t['observations']} observations).",
                t["mean_conf"], ["COCO class of forensic interest"], [cite(t)])
    # R7 machine disagreement
    for tid, ck in checks.items():
        if ck["needs_review"]:
            t = by_id[tid]
            add("R7 models disagree", "review", f"Detector and VLM disagree on {tid}",
                f"Layer 2 says {t['cls']}; the VLM says '{vlm_by_track[tid]['answer'].get('subject_class')}'.",
                0.5, ["disagreement is surfaced, never resolved silently"], [cite(t)])
    # R8 clock caution
    for f in findings:
        if f.get("kind") in ("clock-change", "clock-drift"):
            ch = f["detail"].get("channel")
            ts = [t for t in tracks if t["channel"] == ch]
            if ts:
                add("R8 clock caution", "warning", f"Times on camera {ch} need correction",
                    f"{f['summary']} Times above use the recorder clock, not the on-screen clock.", 0.8,
                    ["layer 4a uses recorder time as the reference"], [cite(t) for t in ts[:3]])
    return out


NARRATIVE_SYSTEM = ("You write the summary section of a forensic CCTV report. Use ONLY the numbered facts given. "
                    "End every sentence with the fact id(s) it relies on in square brackets, e.g. [I2]. Do not add "
                    "names, identities, intent, guilt or anything not in the facts. Use recorder times exactly as given. "
                    "Write 4 to 7 short sentences in plain English. Plain sentences only: no headings, no lists, "
                    "no bold, no markdown.")


def narrative(insights: list, entities: list, model: str = "qwen3-vl:4b") -> dict:
    from . import vlm
    if not insights:      # nothing to summarise: do not let a model write about nothing
        return {"model": model, "ok": True, "system": NARRATIVE_SYSTEM, "prompt": "(not sent: no facts)",
                "raw": "", "seconds": 0, "sentences": [], "verified_share": 1.0,
                "note": "No layer-4 insights, so no narrative was generated."}
    rank = {"critical": 0, "warning": 1, "lead": 2, "review": 3, "info": 4}
    top = sorted(insights, key=lambda i: (rank.get(i["severity"], 5), i["rule"].startswith("R0"), -i["confidence"]))[:14]
    top.sort(key=lambda i: int(i["id"][1:]))
    facts = "\n".join(f"[{i['id']}] ({i['rule']}) {i['statement']}" for i in top) or "[I0] No events were found."
    prompt = f"Facts:\n{facts}\n\nWrite the summary."
    rec = {"model": model, "system": NARRATIVE_SYSTEM, "prompt": prompt}
    try:
        r = vlm.chat([{"role": "system", "content": NARRATIVE_SYSTEM}, {"role": "user", "content": prompt}], model=model)
        rec.update(raw=r["text"], seconds=r["seconds"], ok=True)
    except Exception as e:
        rec.update(ok=False, error=str(e)[:300], sentences=[])
        return rec
    ids = {i["id"]: i for i in insights}
    sentences = []
    clean = re.sub(r"[*#`]+|^\s*[-•]\s+", "", rec["raw"], flags=re.M)
    for s in re.split(r"(?<=[.!?\]])\s+(?=[A-Z])", clean.strip()):
        s = s.strip()
        if not s:
            continue
        cited = []
        for grp in re.findall(r"\[([^\]]*)\]", s):
            for a, b in re.findall(r"I(\d+)(?:\s*[-–]\s*I?(\d+))?", grp):
                cited += [f"I{k}" for k in range(int(a), int(b or a) + 1)]
        valid = [c for c in cited if c in ids]
        problems = []
        if not cited:
            problems.append("no citation")
        if len(valid) < len(cited):
            problems.append("cites a fact that does not exist")
        times = re.findall(r"\b\d{2}:\d{2}:\d{2}\b", s)
        src = " ".join(ids[c]["statement"] for c in valid)
        bad_t = [t for t in times if t not in src]
        if bad_t:
            problems.append(f"time {', '.join(bad_t)} not in cited facts")
        nums = [n for n in re.findall(r"\b\d+\b", re.sub(r"\[[^\]]*\]|\b\d{2}:\d{2}:\d{2}\b", "", s))
                if n not in src and n not in "".join(ids[c]["title"] for c in valid)]
        if nums and valid:
            problems.append(f"number(s) {', '.join(nums[:3])} not in cited facts")
        sentences.append({"text": s, "citations": valid, "verified": not problems, "problems": problems})
    rec["sentences"] = sentences
    rec["verified_share"] = round(sum(x["verified"] for x in sentences) / len(sentences), 2) if sentences else 0
    return rec
