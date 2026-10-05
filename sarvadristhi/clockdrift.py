"""Clock-drift engine: compare each camera's burned-in clock (OCR) with the recorder's
container timestamp. The difference is reported per camera with its spread; inconsistent
differences within one camera suggest the clock was changed during recording."""
import statistics
from collections import defaultdict

from . import ocr
from .models import Finding
from .plugins.generic import es_bytes


def _hms(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S")


def measure(buf, frames, samples_per_channel: int = 24) -> tuple[dict, list[Finding]]:
    if not ocr.available():
        return {"status": "OCR unavailable"}, []
    keys = defaultdict(list)
    for f in frames:
        if f.keyframe and f.time_ms is not None and f.time_source in ("index", "container", "ofni") \
                and f.channel is not None:
            keys[f.channel].append(f)
    for fs in keys.values():
        fs.sort(key=lambda f: f.time_ms)

    def spread(fs, n, skip=()):
        n = min(n, len(fs))
        idx = {int(i * (len(fs) - 1) / max(1, n - 1)) for i in range(n)}
        return [fs[i] for i in sorted(idx) if id(fs[i]) not in skip]

    per, labels, rois = defaultdict(list), defaultdict(list), None
    todo = {ch: spread(fs, samples_per_channel) for ch, fs in keys.items()}
    for round_ in range(2):          # second round: more samples where overlays are hard to read
        picks = [f for fs in todo.values() for f in fs]
        if not picks:
            break
        imgs = ocr.decode_keyframes([es_bytes(buf, f) for f in picks])
        reads, rois = ocr.read_overlays(imgs, rois)
        for f, r in zip(picks, reads):
            if r["clock_s"] is not None:
                per[f.channel].append((f.time_ms, r["clock_s"] - f.time_ms // 1000))
            if r["label"]:
                labels[f.channel].append(r["label"])
        done = {id(f) for f in picks}
        todo = {ch: spread(fs, samples_per_channel * 4, done) for ch, fs in keys.items()
                if len(per[ch]) < 6} if round_ == 0 else {}
    if not per:
        return {"status": "no overlay clock could be read"}, []
    result, findings = {}, []
    for ch in sorted(keys):
        d = [x for _, x in per.get(ch, [])]
        if len(d) < 3:
            result[ch] = {"samples": len(d), "status": "insufficient overlay readings", "overlay_regions": rois}
            continue
        med = statistics.median(d)
        agree = [x for x in d if abs(x - med) <= 2]
        mad = statistics.median(abs(x - med) for x in d)
        outliers = len(d) - len(agree)
        drift = statistics.median(agree)
        result[ch] = {"drift_s": drift, "samples": len(d), "agreeing": len(agree), "mad_s": mad,
                      "overlay_regions": rois}
        if labels.get(ch):
            result[ch]["label"] = ocr.consensus_label(labels[ch])
        if abs(drift) >= 2:
            findings.append(Finding("clock-drift", "warning",
                f"Camera {ch}: burned-in clock is {drift:+.0f} s from the recorder timestamp",
                {"channel": ch, "drift_s": drift, "samples": len(d)}))
        # a clock change is a SUSTAINED step: >= 3 consistent readings on each side with
        # different offsets. Isolated OCR misreads never qualify.
        obs = sorted(per[ch])
        seg = [x for _, x in obs]
        steps = []
        for i in range(3, len(seg) - 2):
            a, b = seg[i - 3:i], seg[i:i + 3]
            if max(a) - min(a) <= 2 and max(b) - min(b) <= 2 and \
                    abs(statistics.median(b) - statistics.median(a)) > 2:
                steps.append({"before_s": statistics.median(a), "after_s": statistics.median(b),
                              "between_ms": [obs[i - 1][0], obs[i][0]]})
        if steps:
            findings.append(Finding("clock-change", "critical",
                f"Camera {ch}: overlay-to-recorder offset changes during recording "
                f"({steps[0]['before_s']:+.0f} s to {steps[0]['after_s']:+.0f} s) between "
                f"{_hms(steps[0]['between_ms'][0])} and {_hms(steps[0]['between_ms'][1])} recorder time: "
                f"possible clock adjustment", {"channel": ch, "steps": steps,
                                                "time": steps[0]["between_ms"][1] // 1000}))
    return result, findings
