"""Run one UI analysis job in its own process: python -m sarvadristhi.jobrun <job.json>

The UI server stays responsive because heavy work (GPU models, OCR, carving) never runs in
its threads. The job reads its form from a JSON file, routes the source to the right
workflow and prints progress lines that the server streams to the browser. Exit code 0 =
done, anything else = failed."""
import json
import sys
from pathlib import Path


def route(source: str) -> str:
    from . import fsrecover, media
    src = Path(source)
    if src.is_dir() or src.suffix.lower() in media.VIDEO_EXT or fsrecover.is_filesystem(str(src)):
        return "video-evidence"
    return "recorder-disk"


def live(form: dict, say) -> int:
    """Setugrahan: pull footage from a running recorder, then analyse what came down."""
    import os
    from .custody import Ledger
    from .live import acquire_live
    case_dir = Path(form["case_dir"])
    case_dir.mkdir(parents=True, exist_ok=True)
    lv = form["live"]
    say("Evidence type: live recorder acquisition")
    say("::stage:: acquire")
    say(f"Connecting to {lv['host']}:{lv['port']} as {lv['user']} ...")
    r = acquire_live(lv["host"], lv["user"], os.environ.get("SARVADRISTHI_LIVE_PASSWORD", ""), case_dir / "live",
                     Ledger(case_dir / "custody.jsonl"), vendor=lv.get("vendor", "auto"),
                     channels=[int(c) for c in lv["channels"]], start=lv["start"], end=lv["end"], port=int(lv["port"]))
    (case_dir / "live.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    dev = r.get("device") or {}
    say(f"Device: {dev.get('Manufacturer', '?')} {dev.get('Model', '')} serial {dev.get('SerialNumber', '?')}; "
        f"clock {json.dumps(r.get('clock'), default=str)[:120]}")
    for d in r["downloads"]:
        say(f"Downloaded channel {d['channel']}: {d['bytes']:,} bytes, sha256 {d['sha256'][:16]}…")
    if not r["downloads"]:
        say("Nothing downloaded for that channel and time window.")
        return 2
    from . import multicase
    multicase.run([{"path": d["file"], "name": f"LIVE-CH{d['channel']:02d}", "mode": "auto"} for d in r["downloads"]],
                  case_dir, form["case_id"], form["examiner"], ai=bool(form.get("ai")), log=say)
    say("DONE")
    return 0


def kasauti(form: dict, say) -> int:
    from .kasauti import run
    k = form["kasauti"]
    root = Path(form["case_dir"]).parent
    rep = run(k["image"], Path(k["events"]), Path(form["case_dir"]), k.get("device") or {}, bool(k.get("physical")),
              float(k.get("recorder_offset_s") or 0), form["examiner"], registry=root / ".kasauti", log=say)
    say(f"Kasautipeeth: mean coverage {rep['mean_coverage']}, logged events detected {rep['events_detected']}")
    say("DONE")
    return 0


def main(job_file: str) -> int:
    form = json.loads(Path(job_file).read_text(encoding="utf-8"))
    say = lambda m: print(m, flush=True)
    if form.get("kind") == "live":
        return live(form, say)
    if form.get("kind") == "kasauti":
        return kasauti(form, say)
    sources = form.get("sources") or [form["source"]]
    if len(sources) > 1 or form.get("scenario"):
        from . import multicase
        root = Path(__file__).resolve().parents[1]
        scen = root / "kit" / "scenario" / "scenario.json" if form.get("scenario") else None
        names = {"DVR-A_hikvision": "DVR-A", "DVR-B_dahua": "DVR-B", "USB_shiftB": "USB"}
        say(f"Evidence type: multi-evidence ({len(sources)} items)")
        multicase.run([{"path": s, "name": names.get(Path(s).stem, Path(s).stem), "mode": form.get("mode", "auto")}
                       for s in sources], Path(form["case_dir"]), form["case_id"], form["examiner"],
                      ai=bool(form.get("ai")), scenario=scen,
                      labels_dir=(root / "kit" / "meva") if scen else None, log=say)
        say("DONE")
        return 0
    kind = route(form["source"])
    say(f"Evidence type: {kind}")
    if kind == "video-evidence":
        from . import media
        media.run(form["source"], Path(form["case_dir"]), form["case_id"], form["examiner"],
                  ai=bool(form.get("ai")), analytics=True, log=say)
    else:
        from .pipeline import run
        run(form["source"], Path(form["case_dir"]), form["case_id"], form["examiner"],
            mode=form.get("mode", "auto"), analytics=bool(form.get("analytics", True)), ai=bool(form.get("ai")), log=say)
    say("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
