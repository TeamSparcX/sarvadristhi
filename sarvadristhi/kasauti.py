"""Recorder validation: validate Sarvadristhi on a real recorder against a handwritten event log.

Protocol (printed in the workbench): record on the DVR while writing down what happens and when
(by a reference clock), note the recorder's clock offset against that reference, delete or format
as planned, then image the disk through a write-blocker and run this kit with the log. The kit runs
the full analysis and scores what came back against the log:

  recording   share of logged seconds with recovered frames on that channel, by evidence level
  formatted   a format/initialise event found near the logged time, and older footage recovered
  deleted     footage recorded before the deletion recovered
  clock_set   a clock-change finding on that channel

Only runs marked as a physical recorder can promote the capability matrix to "real disk"; a run on
a modelled image is a rehearsal and says so. One run measures one device; it is never presented
as a general recovery rate.

Event log CSV columns: channel, camera_name, action, start, end, note
(times as 'YYYY-MM-DD HH:MM:SS' on the reference clock)."""
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = ("channel,camera_name,action,start,end,note\n"
            "1,CAM-01 GATE,recording,2026-10-06 10:00:00,2026-10-06 10:05:00,person walks past at 10:02\n"
            "2,CAM-02 LOBBY,recording,2026-10-06 10:00:00,2026-10-06 10:05:00,\n"
            "1,,deleted,2026-10-06 10:20:00,,deleted 10:00-10:05 of CH1 from the menu\n"
            ",,formatted,2026-10-06 10:30:00,,HDD initialised from the menu\n"
            "2,,clock_set,2026-10-06 10:40:00,,camera 2 clock set back 15 minutes\n")


def _ms(s: str, offset_s: float) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    t = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)   # recorder times use a UTC basis
    return int((t.timestamp() + offset_s) * 1000)


def read_log(path: Path, recorder_offset_s: float = 0.0) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            act = (r.get("action") or "").strip().lower()
            if not act:
                continue
            rows.append({"channel": int(r["channel"]) if (r.get("channel") or "").strip() else None,
                         "camera": (r.get("camera_name") or "").strip(), "action": act,
                         "start_ms": _ms(r.get("start"), recorder_offset_s), "end_ms": _ms(r.get("end"), recorder_offset_s),
                         "note": (r.get("note") or "").strip()})
    return rows


def score(case_dir: Path, log: list[dict]) -> dict:
    case_dir = Path(case_dir)
    c = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    by_ch = defaultdict(list)
    with open(case_dir / "frames.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["time_ms"]:
                by_ch[int(r["channel"]) if r["channel"] else None].append((int(r["time_ms"]), r["evidence_level"]))
    results = []
    for e in log:
        res = {**e, "verdict": "not scorable", "detail": ""}
        if e["action"] == "recording" and e["start_ms"] and e["end_ms"]:
            secs = max(1, (e["end_ms"] - e["start_ms"]) // 1000)
            got = [(t, lv) for t, lv in by_ch.get(e["channel"], []) if e["start_ms"] <= t < e["end_ms"]]
            covered = {(t - e["start_ms"]) // 1000 for t, _ in got}
            levels = defaultdict(int)
            for _, lv in got:
                levels[lv] += 1
            cov = round(len(covered) / secs, 4)
            res.update(coverage=cov, frames=len(got), levels=dict(levels),
                       verdict="recovered" if cov >= 0.9 else "partly recovered" if cov > 0 else "not recovered",
                       detail=f"{len(covered)} of {secs} logged seconds have recovered frames")
        elif e["action"] == "formatted" and e["start_ms"]:
            hit = [f for f in c["findings"] if f["kind"] in ("format-event", "index-reset")]
            near = [f for f in hit if f["kind"] != "format-event" or abs((f["detail"].get("time") or 0) * 1000 - e["start_ms"]) <= 600_000]
            older = sum(1 for fr in by_ch.values() for t, _ in fr if t < e["start_ms"])
            res.update(verdict="detected" if near else "missed",
                       detail=f"{len(near)} matching finding(s); {older:,} frames older than the format recovered")
        elif e["action"] == "deleted" and e["start_ms"]:
            older = [t for t, _ in by_ch.get(e["channel"], []) if t < e["start_ms"]] if e["channel"] else \
                [t for fr in by_ch.values() for t, _ in fr if t < e["start_ms"]]
            res.update(verdict="footage recovered" if older else "nothing older recovered",
                       detail=f"{len(older):,} frames recorded before the deletion recovered")
        elif e["action"] == "clock_set":
            hit = [f for f in c["findings"] if f["kind"] == "clock-change"
                   and (e["channel"] is None or f["detail"].get("local_channel", f["detail"].get("channel")) == e["channel"])]
            res.update(verdict="detected" if hit else "missed", detail=hit[0]["summary"] if hit else "no clock-change finding")
        results.append(res)
    recs = [r for r in results if r["action"] == "recording" and "coverage" in r]
    events = [r for r in results if r["action"] in ("formatted", "clock_set")]
    return {"family": c["detection"]["family"], "confidence": c["detection"]["confidence"], "results": results,
            "mean_coverage": round(sum(r["coverage"] for r in recs) / len(recs), 4) if recs else None,
            "events_detected": f"{sum(r['verdict'] == 'detected' for r in events)}/{len(events)}" if events else "none logged"}


def run(image: str, events: Path, out_dir: Path, device: dict, physical: bool, recorder_offset_s: float = 0.0,
        examiner: str = "", registry: Path | None = None, log=print) -> dict:
    from .pipeline import run as analyse
    out_dir = Path(out_dir)
    lg = read_log(Path(events), recorder_offset_s)
    log(f"Event log: {len(lg)} entries")
    case_dir = out_dir / "case"
    analyse(image, case_dir, f"KASAUTI-{time.strftime('%Y%m%d-%H%M%S')}", examiner or "kasautipeeth", pdf=False, log=log)
    s = score(case_dir, lg)
    rep = {"kit": "Recorder validation 1", "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "image": str(image),
           "device": device, "physical_recorder": bool(physical), "recorder_offset_s": recorder_offset_s,
           "examiner": examiner, "case_dir": str(case_dir), **s,
           "scope": ("One recording session on one physical device." if physical else
                     "Rehearsal on a modelled image: exercises the kit, says nothing about real hardware.")}
    (out_dir / "kasauti.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    md = [f"# Recorder validation validation · {device.get('make', '?')} {device.get('model', '')}".rstrip(),
          "", f"{'Physical recorder' if physical else 'REHEARSAL on a modelled image'} · {rep['utc']} · detected family "
          f"{s['family']} ({s['confidence']}) · mean coverage {s['mean_coverage']} · logged events detected {s['events_detected']}",
          "", "| Channel | Action | Logged | Verdict | Detail |", "|---|---|---|---|---|"]
    for r in s["results"]:
        when = datetime.fromtimestamp(r["start_ms"] / 1000, timezone.utc).strftime("%H:%M:%S") if r["start_ms"] else ""
        md.append(f"| {r['channel'] or ''} | {r['action']} | {when} | {r['verdict']} | {r['detail']} |")
    md += ["", rep["scope"]]
    (out_dir / "kasauti.md").write_text("\n".join(md), encoding="utf-8")
    if registry:
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(
            {k: rep[k] for k in ("utc", "device", "physical_recorder", "family", "confidence", "mean_coverage",
                                 "events_detected", "image", "scope")} | {"report": str(out_dir / "kasauti.md")}, indent=1),
            encoding="utf-8")
    return rep


def runs(registry: Path) -> list[dict]:
    if not Path(registry).exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) | {"id": p.stem} for p in sorted(Path(registry).glob("*.json"), reverse=True)]


def promote(caps: dict, registry: Path) -> dict:
    """Mark capability cells 'real' for families with a passing physical-recorder run."""
    fam_ok = {r["family"] for r in runs(registry) if r.get("physical_recorder") and (r.get("mean_coverage") or 0) >= 0.9}
    for o in caps["oems"]:
        if o["family"] in fam_ok:
            for k in ("detect", "recover", "time"):
                o["cells"][k] = "real"
            o["trust"] = "L3"
    caps["kasauti_runs"] = len(runs(registry))
    return caps


def events_from_ground_truth(gt_csv: Path, out_csv: Path, format_time_ms: int | None = None) -> Path:
    """Rehearsal helper: turn a Pramanshala ground-truth file into an event log as an examiner would write it."""
    span = defaultdict(lambda: [None, None])
    with open(gt_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["origin"] != "original":
                continue
            ch, t = int(r["channel"]), int(r["true_time_ms_utc"])
            a = span[ch]
            a[0] = t if a[0] is None else min(a[0], t)
            a[1] = t if a[1] is None else max(a[1], t)
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["channel,camera_name,action,start,end,note"]
    for ch, (a, b) in sorted(span.items()):
        lines.append(f"{ch},,recording,{fmt(a)},{fmt(b)},rehearsal from ground truth")
    if format_time_ms:
        lines.append(f",,formatted,{fmt(format_time_ms)},,rehearsal")
    Path(out_csv).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(out_csv)
