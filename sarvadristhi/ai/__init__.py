"""sarvadristhi AI layer: four stacked layers, each one inspectable.

  L1 motion       analytics.motion   - when does something move (cheap, every frame sampled)
  L2 perception   ai.perception      - what is it, which detections are the same object
  L3 understanding ai.vlm            - what is it doing / wearing, checked against L2
  L4 context      ai.reasoner        - same object across cameras, rules, cited narrative

run() returns one JSON document with every layer's output AND a step-by-step trace of what
each layer was given, what it did, with which model and parameters, and how long it took.
The UI renders this document as the "AI reasoning" view; nothing is hidden from the examiner.
All AI output is labelled as leads for a human, never as findings of fact."""
import json
import time
from pathlib import Path

from . import activities, perception, reasoner, vlm
from .reasoner import hhmmss


def _strip(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}


def run(case_dir: Path, clips: list[dict], events: list[dict], findings: list[dict], log=print,
        use_vlm: bool = True, use_llm: bool = True, sample_fps: float = 4.0, max_vlm: int = 10) -> dict:
    case_dir = Path(case_dir)
    thumbs = case_dir / "ai" / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    trace = []

    def step(layer, name, what, **kw):
        trace.append({"layer": layer, "step": name, "what": what, **kw})
        log(f"  [{layer}] {name}: {what}")

    usable = [c for c in clips if c.get("mp4_file") and c.get("start_ms") is not None]
    for c in usable:
        c["channel_name"] = c.get("label") or (f"Camera {c['channel']}" if c.get("channel") is not None else c["id"])

    # ---------------------------------------------------------------- L1
    log("::stage:: l1")
    t0 = time.time()
    by_clip = {}
    for e in events:
        by_clip.setdefault(e["clip"], []).append(e)
    step("L1", "motion", f"{len(events)} motion event(s) in {len(by_clip)} clip(s) from background subtraction",
         method="OpenCV MOG2, overlay text masked, compact-blob filter", seconds=round(time.time() - t0, 2))

    # ---------------------------------------------------------------- L2
    log("::stage:: l2")
    t0 = time.time()
    det = perception.Detector()
    faces = perception.Faces()
    step("L2", "models", f"object detector: {det.backend} on {det.device}; faces: "
         f"{'YuNet + SFace' if faces.ok else 'unavailable'}", error=det.error,
         params={"threshold": det.threshold, "sample_fps": sample_fps, "classes": sorted(perception.KEEP)},
         seconds=round(time.time() - t0, 2))
    per_clip, tracks = [], []
    for c in usable:
        r = perception.perceive(c, str(case_dir / "clips" / c["mp4_file"]), det, faces, thumbs, sample_fps)
        for t in r["tracks"]:
            t["clip_dur"] = r["frames_examined"] / r["sample_fps"]
            t["channel_name"] = c["channel_name"]
            t["abs_start_ms"] = c["start_ms"] + int(t["first_t"] * 1000)
            t["abs_end_ms"] = c["start_ms"] + int(t["last_t"] * 1000)
            t["abs_best_ms"] = c["start_ms"] + int(t["best_t"] * 1000)
            t["time"] = f"{hhmmss(t['abs_start_ms'])}-{hhmmss(t['abs_end_ms'])}"
        tracks += r["tracks"]
        per_clip.append(r)
        counts = {}
        for t in r["tracks"]:
            counts[t["cls"]] = counts.get(t["cls"], 0) + 1
        step("L2", f"perceive {c['id']}", f"{r['frames_examined']} frames, {len(r['detections'])} detections -> "
             f"{len(r['tracks'])} tracks {counts}; {r['noise_tracks_dropped']} single-frame blips dropped",
             clip=c["id"], seconds=r["seconds"])
    # L1 x L2: is each motion event explained by a tracked object?
    for e in events:
        hit = [t for t in tracks if t["clip"] == e["clip"] and t["first_t"] <= e["end_s"] and t["last_t"] >= e["start_s"]]
        e["explained_by"] = [t["id"] for t in hit]
    unexplained = [e for e in events if not e["explained_by"]]
    if events:
        step("L1xL2", "explain motion", f"{len(events) - len(unexplained)} of {len(events)} motion events overlap a "
             f"tracked object; {len(unexplained)} unexplained (lighting, shadow, or an object the detector missed)")

    # ---------------------------------------------------------------- L3
    log("::stage:: l3")
    descriptions, checks = [], {}
    ok, why = vlm.available()
    if use_vlm and ok:
        cand = sorted([t for t in tracks if t["cls"] in ("person", "car", "truck", "bus", "motorcycle", "bicycle",
                                                          "backpack", "suitcase", "handbag", "knife")],
                      key=lambda t: -(t["observations"] * t["max_conf"]))
        picked, seen_clip = [], set()
        for t in cand:                          # at least the strongest track per clip, then by strength
            if t["clip"] not in seen_clip:
                picked.append(t)
                seen_clip.add(t["clip"])
        for t in cand:
            if len(picked) >= max_vlm:
                break
            if t not in picked:
                picked.append(t)
        step("L3", "select", f"{len(picked)} of {len(tracks)} tracks sent to {vlm.MODEL} (strongest per clip, then "
             f"by observations x confidence; limit {max_vlm})")
        for t in picked:
            frame = (case_dir / t["thumb"]).read_bytes()
            crop_p = case_dir / t["crop"]
            d = vlm.describe_track(t, t["channel_name"], hhmmss(t["abs_best_ms"]), frame,
                                   crop_p.read_bytes() if crop_p.exists() else None)
            if d.get("ok"):
                d["cross_check"] = checks[t["id"]] = vlm.cross_check(t, d["answer"])
            descriptions.append(d)
            step("L3", f"describe {t['id']}", (f"'{d['answer'].get('action', '')}' - agreement "
                 f"{d['cross_check']['agreement']:.0%}" if d.get("ok") else f"failed: {d.get('error')}"),
                 seconds=d.get("seconds"))
    else:
        step("L3", "skipped", why if use_vlm else "disabled by option")
    vlm_by_track = {d["track"]: d for d in descriptions if d.get("ok")}

    # ---------------------------------------------------------------- L4
    log("::stage:: l4")
    t0 = time.time()
    drifts = {f["detail"].get("channel"): f["detail"].get("drift_s") for f in findings if f.get("kind") == "clock-drift"}
    step("L4", "normalise time", f"{len(tracks)} tracks placed on the recorder clock; overlay drift known for "
         f"{len(drifts)} camera(s) {drifts or ''}".strip())
    cands, links, entities = reasoner.correlate(tracks, vlm_by_track)
    step("L4", "correlate", f"{len(cands)} cross-camera candidate pairs scored; {len(links)} accepted at >= "
         f"{reasoner.LINK_THRESHOLD}; {len(entities)} distinct entities "
         f"({sum(len(e['cameras']) > 1 for e in entities)} seen on more than one camera)",
         weights={"appearance": reasoner.W_APPEAR, "time": reasoner.W_TIME, "geometry": reasoner.W_GEOM,
                  "vlm": reasoner.W_VLM, "face": reasoner.W_FACE})
    acts = activities.detect(tracks)
    counts = {}
    for a in acts:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    step("L4", "activity rules", f"{len(acts)} activity events {counts}",
         rules="enter/exit vehicle, vehicle starts/stops/moving, person enters/exits through a structure")
    insights = reasoner.rules(tracks, entities, usable, findings, vlm_by_track, checks)
    by_track = {t["id"]: t for t in tracks}
    shown = {"enter_vehicle": 6, "exit_vehicle": 6, "vehicle_starts": 4, "vehicle_stops": 4, "person_enters": 4,
             "person_exits": 4}
    for a in acts:
        if shown.get(a["type"], 0) <= 0:
            continue
        shown[a["type"]] -= 1
        t = by_track[a["track"]]
        what = {"enter_vehicle": "A person gets into a vehicle", "exit_vehicle": "A person gets out of a vehicle",
                "vehicle_starts": "A parked vehicle starts moving", "vehicle_stops": "A moving vehicle stops",
                "person_enters": "A person leaves view inside the scene (door or structure)",
                "person_exits": "A person appears inside the scene (door or structure)"}[a["type"]]
        insights.append({"id": f"I{len(insights) + 1}", "rule": f"R9 activity: {a['type']}",
                         "severity": "lead", "title": f"{what} on {a['camera']}",
                         "statement": f"{what} on {a['camera']} at {hhmmss(a['from_ms'] + 1000)}"
                                      + (f" (vehicle {a['vehicle']})" if a.get("vehicle") else "") + ".",
                         "confidence": a["confidence"], "reasons": [a["why"]],
                         "citations": [{"clip": t["clip"], "camera": t["channel_name"], "t_s": a["t_s"],
                                        "time": hhmmss(a["from_ms"] + 1000), "track": t["id"], "sha256": "",
                                        "thumb": t["thumb"]}], "activity": a["id"]})
    step("L4", "context rules", f"{len(insights)} insight(s) from rules R1-R8",
         fired=sorted({i["rule"] for i in insights}), seconds=round(time.time() - t0, 2))
    story = {}
    if use_llm:
        story = reasoner.narrative(insights, entities)
        if story.get("ok"):
            step("L4", "narrative", f"{len(story['sentences'])} sentences by {story['model']}; "
                 f"{sum(s['verified'] for s in story['sentences'])} pass the citation/time/number check",
                 seconds=story.get("seconds"))
        else:
            step("L4", "narrative", f"skipped: {story.get('error')}")

    out = {"layers": {
        "L1": {"name": "Motion", "events": events, "unexplained": len(unexplained)},
        "L2": {"name": "Perception", "backend": det.backend, "device": det.device,
               "faces": "YuNet + SFace" if faces.ok else None, "clips": [
                   {**{k: v for k, v in r.items() if k != "tracks"}} for r in per_clip],
               "tracks": [_strip(t) for t in tracks]},
        "L3": {"name": "Scene understanding", "model": vlm.MODEL, "available": ok, "status": why,
               "descriptions": descriptions},
        "L4": {"name": "Context & decisions", "candidates": cands, "links": links, "entities": entities,
               "insights": insights, "narrative": story, "threshold": reasoner.LINK_THRESHOLD,
               "activities": acts}},
        "trace": trace,
        "disclaimer": "AI output is investigative leads for a human examiner. It is not evidence by itself and "
                      "is excluded from the s.63(4) data sheet. Model accuracy on this footage is not validated."}
    (case_dir / "ai" / "ai.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out
