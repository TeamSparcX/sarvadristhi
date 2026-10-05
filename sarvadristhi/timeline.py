"""Cross-camera timeline: clips, triage events, recorder logs and findings on one axis."""
from collections import defaultdict

from .models import Finding

GAP_S = 5


def recording_gaps(clips) -> list[Finding]:
    by = defaultdict(list)
    for c in clips:
        if c.channel is not None and c.start_ms is not None:
            by[c.channel].append(c)
    out = []
    for ch, cs in by.items():
        cs.sort(key=lambda c: c.start_ms)
        for a, b in zip(cs, cs[1:]):
            gap = (b.start_ms - a.end_ms) / 1000
            if gap > GAP_S:
                out.append(Finding("recording-gap", "info",
                    f"Camera {ch}: no recovered video for {gap:.0f} s between {a.id} and {b.id}",
                    {"channel": ch, "from_ms": a.end_ms, "to_ms": b.start_ms, "seconds": gap}))
    return out


def build(clips, events, logs, findings) -> dict:
    items = []
    for c in clips:
        if c.start_ms is not None:
            items.append({"kind": "clip", "channel": c.channel, "start_ms": c.start_ms, "end_ms": c.end_ms,
                          "id": c.id, "level": c.level.label})
    for e in events:
        items.append({"kind": "event", **e, "start_ms": e["abs_start_ms"]})
    for l in logs:
        items.append({"kind": "log", "start_ms": l["time"] * 1000, "text": l["text"], "type": l["type_name"]})
    for f in findings:
        t = f.detail.get("time") if isinstance(f.detail, dict) else None
        if t:
            items.append({"kind": "finding", "start_ms": t * 1000, "text": f.summary, "severity": f.severity})
    items.sort(key=lambda x: x["start_ms"])
    return {"items": items, "channels": sorted({c.channel for c in clips if c.channel is not None})}
