"""Layer 4e - activity rules: turn tracks into events a CCTV examiner asks about.

Each rule is geometry over layer-2 tracks, stated in full so it can be checked:
  enter_vehicle    a person track ENDS with its centre inside a vehicle box (+40 % margin)
                   while that vehicle is in view
  exit_vehicle     a person track STARTS inside a vehicle box
  vehicle_starts   a vehicle stationary >= 2 s, then moving
  vehicle_stops    a vehicle moving, then stationary >= 2 s
  vehicle_moving   a vehicle moving >= 1 s (speed > 0.15 box widths per second)
  person_enters    a person track ENDS away from every frame edge and every vehicle
                   (a door, a gate, a structure) - weaker: occlusion produces the same pattern
  person_exits     a person track STARTS away from every frame edge and every vehicle
Events are leads with the rule, inputs and confidence; they are not findings."""
import numpy as np

VEHICLES = {"car", "truck", "bus", "motorcycle"}


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def _box_at(track, t):
    return min(track["path"], key=lambda p: abs(p[0] - t))[1]


def _inside(pt, box, margin=0.4):
    w, h = box[2] - box[0], box[3] - box[1]
    return box[0] - margin * w <= pt[0] <= box[2] + margin * w and box[1] - margin * h <= pt[1] <= box[3] + margin * h


def _interior(box, size, m=0.08):
    w, h = size
    cx, cy = _centre(box)
    return m * w < cx < (1 - m) * w and m * h < cy and box[3] < (1 - m / 2) * h


def detect(tracks: list[dict]) -> list[dict]:
    out = []
    by_clip = {}
    for t in tracks:
        by_clip.setdefault(t["clip"], []).append(t)
    for clip, ts in by_clip.items():
        veh = [v for v in ts if v["cls"] in VEHICLES and v["observations"] >= 4]
        people = [p for p in ts if p["cls"] == "person" and p["observations"] >= 3]

        dur = max((t.get("clip_dur") or 0) for t in ts) or max(t["last_t"] for t in ts)

        def at_edge_of_clip(t_s):              # a track ending because the clip ends is not an exit
            return t_s < 1.0 or t_s > dur - 1.0

        def fragment(p, t_s, end):
            """Another person track continues (end) / precedes (start) nearby within 2 s: occlusion split."""
            pb = _box_at(p, t_s)
            hgt = max(1.0, pb[3] - pb[1])
            for q in people:
                if q is p:
                    continue
                qt = q["first_t"] if end else q["last_t"]
                if (0 < qt - t_s <= 2.0) if end else (0 < t_s - qt <= 2.0):
                    qb = _box_at(q, qt)
                    if np.hypot(*(np.array(_centre(qb)) - np.array(_centre(pb)))) < 1.5 * hgt:
                        return True
            return False

        def departs(v, t_s):
            pts = sorted(v["path"])
            later = [p for p in pts if t_s <= p[0] <= t_s + 60]
            if len(later) < 2:
                return False
            a, b = np.array(_centre(later[0][1])), np.array(_centre(later[-1][1]))
            return np.hypot(*(b - a)) > 0.5 * max(8.0, later[0][1][2] - later[0][1][0])

        def ev(kind, t, a_s, b_s, conf, why, **kw):
            base = t["abs_start_ms"] - int(t["first_t"] * 1000)
            out.append({"type": kind, "track": t["id"], "clip": clip, "camera": t["channel_name"],
                        "channel": t["channel"], "from_ms": base + int(a_s * 1000), "to_ms": base + int(b_s * 1000),
                        "t_s": a_s, "confidence": round(conf, 2), "why": why, **kw})
        for p in people:
            for end, kind, t_s in ((True, "enter_vehicle", p["last_t"]), (False, "exit_vehicle", p["first_t"])):
                if at_edge_of_clip(t_s) or fragment(p, t_s, end):
                    continue
                pb = _box_at(p, t_s)
                for v in veh:
                    if not (v["first_t"] - 1 <= t_s <= v["last_t"] + 1):
                        continue
                    vb = _box_at(v, t_s)
                    if _inside(_centre(pb), vb, 0.25) and vb[1] - 0.25 * (vb[3] - vb[1]) <= pb[3] <= vb[3] + 0.25 * (vb[3] - vb[1]):
                        moved = departs(v, t_s) if end else False
                        ev(kind, p, t_s - 1, t_s + 1, (0.7 * p["mean_conf"] + 0.3 * v["mean_conf"]) * (1.0 if moved else 0.75),
                           f"person track {'ends' if end else 'starts'} at {t_s:.1f} s at vehicle {v['id']} (centre in box +25 %, "
                           f"feet at the vehicle){'; the vehicle then moves' if moved else ''}", vehicle=v["id"],
                           confirmed=moved)
                        break
            # door / structure: track ends (starts) in the interior, not beside a vehicle
            for end, kind, t_s in ((True, "person_enters", p["last_t"]), (False, "person_exits", p["first_t"])):
                if at_edge_of_clip(t_s) or fragment(p, t_s, end) or p["observations"] < 6 or p["duration_s"] < 2:
                    continue
                pb = _box_at(p, t_s)
                near_v = any(v["first_t"] - 1 <= t_s <= v["last_t"] + 1 and _inside(_centre(pb), _box_at(v, t_s), 0.8)
                             for v in veh)
                if p["duration_s"] >= 1.0 and _interior(pb, p["frame_size"]) and not near_v:
                    ev(kind, p, t_s - 1, t_s + 1, 0.4 * p["mean_conf"],
                       f"person track {'ends' if end else 'starts'} at {t_s:.1f} s away from frame edges and vehicles "
                       f"(door/structure, or occlusion)")
        for v in veh:
            pts = sorted(v["path"])
            if len(pts) < 4:
                continue
            ts_ = np.array([p[0] for p in pts])
            cs = np.array([_centre(p[1]) for p in pts])
            wd = np.array([max(8.0, p[1][2] - p[1][0]) for p in pts])
            sp = np.zeros(len(pts))
            sp[1:] = np.hypot(*(cs[1:] - cs[:-1]).T) / np.maximum(ts_[1:] - ts_[:-1], 1e-3) / wd[1:]
            moving = sp > 0.15
            i = 0
            while i < len(pts):
                j = i
                while j + 1 < len(pts) and moving[j + 1] == moving[i]:
                    j += 1
                dur = ts_[j] - ts_[i]
                if moving[i] and dur >= 1.0:
                    ev("vehicle_moving", v, ts_[i], ts_[j], v["mean_conf"] * min(1.0, 0.5 + dur / 6),
                       f"centre moves {float(sp[i:j + 1].mean()):.2f} box widths/s for {dur:.1f} s")
                    prev = [k for k in range(i) if moving[k]]
                    still_since = ts_[prev[-1]] if prev else ts_[0]   # stationary from here until i
                    if i > 0 and ts_[i] - still_since >= 2:
                        ev("vehicle_starts", v, ts_[i] - 1, ts_[i] + 1, v["mean_conf"] * 0.8,
                           f"stationary >= 2 s, then moving at {ts_[i]:.1f} s")
                    nxt = [k for k in range(j + 1, len(pts)) if moving[k]]
                    if j + 1 < len(pts) and (ts_[nxt[0]] if nxt else ts_[-1]) - ts_[j] >= 2:
                        ev("vehicle_stops", v, ts_[j] - 1, ts_[j] + 1, v["mean_conf"] * 0.8,
                           f"moving, then stationary >= 2 s from {ts_[j]:.1f} s")
                i = j + 1
    out.sort(key=lambda e: e["from_ms"])
    for k, e in enumerate(out, 1):
        e["id"] = f"A{k}"
    return out
