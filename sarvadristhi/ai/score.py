"""Score layer-4 activity events against human labels (MEVA activities.yml).

A label is SCORABLE only if the case actually holds footage of that camera at that time
(recovered from a recorder or from an export); labels that fall in destroyed footage are
reported as such. A label is HIT when an event of a mapped type overlaps it (+/- 2 s).
Recall = hits / scorable modelled labels. MEVA labels only scripted activities, so events
with no label are 'unlabelled', not necessarily false: precision is reported as a lower bound."""
import re
from datetime import datetime, timezone
from pathlib import Path

MAP = {"Enter_Vehicle": {"enter_vehicle"}, "Open_Vehicle_Door": {"enter_vehicle", "exit_vehicle"},
       "Exit_Vehicle": {"exit_vehicle"}, "Close_Vehicle_Door": {"enter_vehicle", "exit_vehicle"},
       "Vehicle_Starting": {"vehicle_starts", "vehicle_moving"}, "Vehicle_Stopping": {"vehicle_stops", "vehicle_moving"},
       "Vehicle_Reversing": {"vehicle_moving"}, "Vehicle_Turning_Left": {"vehicle_moving"},
       "Vehicle_Turning_Right": {"vehicle_moving"}, "Enter_Facility": {"person_enters"},
       "Open_Facility_Door": {"person_enters", "person_exits"}, "Exit_Facility": {"person_exits"}}
TOL_MS = 2000


def load_labels(labels_dir: Path, source: str) -> list[dict]:
    """source e.g. 'school.G328' -> labels with absolute times (file start from its name, 30 fps)."""
    out = []
    for f in Path(labels_dir).glob(f"*.{source}.activities.yml"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.(\d{2})-(\d{2})-(\d{2})\.", f.name)
        start = datetime(*map(int, m.group(1).split("-")), int(m.group(2)), int(m.group(3)), int(m.group(4)),
                         tzinfo=timezone.utc).timestamp() * 1000
        txt = f.read_text(encoding="utf-8")
        for name, a, b in re.findall(r"act3: *\{ *([A-Za-z_]+):[^}]*\}.*?tsr0: *\[(\d+), *(\d+)\]", txt):
            out.append({"activity": name, "from_ms": int(start + int(a) / 30 * 1000),
                        "to_ms": int(start + int(b) / 30 * 1000), "source": source, "file": f.name})
    return out


def score(events: list[dict], labels_by_camera: dict, coverage: dict) -> dict:
    """labels_by_camera: camera name -> labels; coverage: camera name -> [(from_ms, to_ms)] footage held."""
    rows, used = [], set()
    for cam, labels in labels_by_camera.items():
        for l in labels:
            held = any(a <= l["to_ms"] and l["from_ms"] <= b for a, b in coverage.get(cam, []))
            row = {**l, "camera": cam}
            if l["activity"] not in MAP:
                row["status"] = "not modelled"
            elif not held:
                row["status"] = "footage destroyed / not recovered"
            else:
                hit = [e for e in events if e["camera"] == cam and e["type"] in MAP[l["activity"]]
                       and e["from_ms"] <= l["to_ms"] + TOL_MS and l["from_ms"] - TOL_MS <= e["to_ms"]]
                row["status"] = "hit" if hit else "missed"
                row["matched_events"] = [e["id"] for e in hit]
                used |= {e["id"] for e in hit}
            rows.append(row)
    scorable = [r for r in rows if r["status"] in ("hit", "missed")]
    hits = sum(r["status"] == "hit" for r in rows)
    cams = set(labels_by_camera)
    evs = [e for e in events if e["camera"] in cams]
    return {"labels": rows, "labels_total": len(rows), "scorable": len(scorable), "hits": hits,
            "recall": round(hits / len(scorable), 2) if scorable else None,
            "events_on_labelled_cameras": len(evs), "events_matching_a_label": len(used),
            "precision_lower_bound": round(len(used) / len(evs), 2) if evs else None,
            "not_modelled": sum(r["status"] == "not modelled" for r in rows),
            "destroyed": sum(r["status"].startswith("footage destroyed") for r in rows),
            "method": "event overlaps label +/- 2 s; MEVA labels cover scripted activities only, so unlabelled "
                      "events lower the precision bound without being proven false"}
