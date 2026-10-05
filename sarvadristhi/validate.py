"""Score recovery against an evidence_builder build (frame-level ground truth).

Metrics, per image and per mode:
  recovery rate      recovered surviving frames / surviving frames in ground truth
  false positives    recovered units that are not a surviving ground-truth frame
  temporal accuracy  frames whose recovered time is within 1 s of truth
                     (native: recorder time; universal: the clock the camera displayed)
  camera accuracy    frames attributed to the right camera
  index accuracy     Hikvision: index-confirmed level matches ground truth exactly
"""
import bisect
import csv
import json
import time
from pathlib import Path

from .models import EvidenceLevel
from .recover import recover
from .source import Source


def _score(rows, frames, universal: bool) -> dict:
    live = [r for r in rows if r["status"] == "intact"]
    exact = {(int(r["offset"]), int(r["length"])): r for r in live}
    spans = sorted((int(r["offset"]), int(r["offset"]) + int(r["length"]), r) for r in live)
    starts = [a for a, _, _ in spans]
    hit, fp = {}, 0
    for f in frames:
        if not universal:
            r = exact.get((f.offset, f.length))
        else:
            i = bisect.bisect_right(starts, f.offset) - 1
            r = spans[i][2] if i >= 0 and spans[i][0] <= f.offset < spans[i][1] else None
        if r is None:
            fp += 1
        else:
            hit[(r["offset"], r["length"])] = (f, r)
    key = "overlay_time_ms_utc" if universal else "true_time_ms_utc"
    timed = [(f, r) for f, r in hit.values() if f.time_ms is not None]
    t_ok = sum(1 for f, r in timed if abs(f.time_ms - int(r[key])) <= 1000)
    attributed = [(f, r) for f, r in hit.values() if f.channel is not None]
    # attribution clusters (e.g. sensor fingerprints) carry arbitrary ids: map each to the
    # ground-truth camera most of its frames belong to (standard clustering accuracy)
    gt_ch = {int(r["channel"]) for r in live}
    from collections import Counter, defaultdict
    votes = defaultdict(Counter)
    for f, r in attributed:
        if f.channel not in gt_ch:
            votes[f.channel][int(r["channel"])] += 1
    remap = {c: v.most_common(1)[0][0] for c, v in votes.items()}
    ch_ok = sum(1 for f, r in attributed if remap.get(f.channel, f.channel) == int(r["channel"]))
    out = {"surviving_frames": len(live), "recovered": len(hit),
           "recovery_rate": round(len(hit) / max(1, len(live)), 4),
           "false_positives": fp, "with_time": len(timed),
           "temporal_accuracy": round(t_ok / max(1, len(timed)), 4),
           "attributed": len(attributed), "unattributed": len(hit) - len(attributed),
           "camera_accuracy": round(ch_ok / max(1, len(attributed)), 4)}
    if "index_confirmed" in live[0] and not universal:
        ok = sum(1 for f, r in hit.values()
                 if (f.level == EvidenceLevel.INDEX_CONFIRMED) == (r["index_confirmed"] == "1"))
        out["index_level_accuracy"] = round(ok / max(1, len(hit)), 4)
    return out


def validate(testkit_out: Path, universal: bool = False, out: Path | None = None,
             only: list[str] | None = None) -> dict:
    """only: restrict to these image names; results merge into an existing out/validation.json."""
    m = json.loads((testkit_out / "manifest.json").read_text())
    results = {"testkit_version": m["kit_version"], "images": {}}
    if out and (out / "validation.json").exists():
        prev = json.loads((out / "validation.json").read_text())
        if prev.get("testkit_version") == m["kit_version"]:
            results["images"] = prev["images"]
    for name in m["images"]:
        if only and name not in only and Path(name).stem not in only:
            continue
        rows = list(csv.DictReader((testkit_out / "ground_truth" / f"{Path(name).stem}.csv").open()))
        for mode in (["auto", "universal"] if universal else ["auto"]):
            with Source(str(testkit_out / "images" / name)) as src:
                t = time.time()
                det, rec = recover(src, mode=mode)
                dt = time.time() - t
            s = _score(rows, rec.frames, mode == "universal")
            s.update(family=det["family"], seconds=round(dt, 1),
                     findings=sorted({f.kind for f in rec.findings}))
            results["images"][f"{name} [{'native' if mode == 'auto' else 'universal'}]"] = s
            print(f"{name:28} {('native' if mode == 'auto' else 'universal'):9} recovered "
                  f"{s['recovered']}/{s['surviving_frames']} fp {s['false_positives']} "
                  f"time {s['temporal_accuracy']:.2%} camera {s['camera_accuracy']:.2%} ({dt:.0f}s)")
    md = markdown(results)
    results["markdown"] = md
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "validation.json").write_text(json.dumps(results, indent=2))
        (out / "validation.md").write_text(
            f"# Validation on evidence_builder {m['kit_version']}\n\nSynthetic content in vendor-exact containers. "
            f"Universal mode ignores vendor parsers and times frames from the burned-in clock, so its "
            f"temporal accuracy is scored against the time the camera displayed.\n\n" + results["markdown"] + "\n",
            encoding="utf-8")  # Windows: "±" must be written as UTF-8, not cp1252
    return results


def markdown(results: dict) -> str:
    pct = lambda v, n: f"{v:.2%}" if n else "n/a"
    md = ["| Image | Mode | Recovered | Recovery rate | False positives | Timed | Temporal accuracy (±1 s) | Camera accuracy (attributed) |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, s in sorted(results["images"].items(), key=lambda kv: (kv[0].endswith("[universal]"), kv[0])):
        img, mode = k.rsplit(" [", 1)
        md.append(f"| {img} | {mode[:-1]} | {s['recovered']}/{s['surviving_frames']} | {s['recovery_rate']:.2%} | "
                  f"{s['false_positives']} | {s['with_time']} | {pct(s['temporal_accuracy'], s['with_time'])} | "
                  f"{pct(s['camera_accuracy'], s['attributed'])} ({s['attributed']}) |")
    return "\n".join(md)
