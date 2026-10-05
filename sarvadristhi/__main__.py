import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(prog="python -m sarvadristhi", description="Multi-vendor DVR/NVR forensic analysis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="full case run on an image or device")
    a.add_argument("source", help="disk image, export file, or block device (read-only)")
    a.add_argument("--case", required=True, type=Path, help="case output directory")
    a.add_argument("--case-id", default="CASE-001")
    a.add_argument("--examiner", default="unnamed examiner")
    a.add_argument("--acquire-to", type=Path, help="image the source here first, then analyse the image")
    a.add_argument("--acquire-format", choices=["raw", "e01"], default="raw",
                   help="e01: EnCase image via ewfacquire (path given without extension)")
    a.add_argument("--ai", action="store_true", help="run optional AI triage adapters if installed")
    a.add_argument("--mode", choices=["auto", "universal"], default="auto",
                   help="universal = ignore vendor parsers (Tier 3 path)")
    a.add_argument("--no-export", action="store_true")
    a.add_argument("--no-analytics", action="store_true")
    a.add_argument("--no-overlay", action="store_true", help="skip overlay OCR")
    a.add_argument("--no-pdf", action="store_true")

    d = sub.add_parser("detect", help="identify the recorder format family")
    d.add_argument("source")

    q = sub.add_parser("acquire", help="bit-for-bit image with hashes and custody entry")
    q.add_argument("source")
    q.add_argument("image", type=Path)
    q.add_argument("--case", required=True, type=Path)
    q.add_argument("--format", choices=["raw", "e01"], default="raw")

    lv = sub.add_parser("live", help="pull recordings from a running recorder over the network")
    lv.add_argument("--host", required=True)
    lv.add_argument("--port", type=int, default=80)
    lv.add_argument("--user", required=True)
    lv.add_argument("--password", required=True)
    lv.add_argument("--vendor", choices=["auto", "dahua", "hikvision"], default="auto")
    lv.add_argument("--channels", default="1", help="comma-separated, e.g. 1,2,3")
    lv.add_argument("--start", required=True, help="'YYYY-MM-DD HH:MM:SS' recorder time")
    lv.add_argument("--end", required=True)
    lv.add_argument("--case", required=True, type=Path)
    lv.add_argument("--analyze", action="store_true", help="run the full analysis on each download")

    ui = sub.add_parser("ui", help="local examiner UI in the browser (127.0.0.1 only)")
    ui.add_argument("--cases", type=Path, default=Path("cases"))
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")

    v = sub.add_parser("verify-custody", help="check a case's hash-chained custody ledger")
    v.add_argument("case", type=Path)

    t = sub.add_parser("validate", help="score recovery against a dvr-pramanshala build")
    t.add_argument("testkit_out", type=Path)
    t.add_argument("--universal", action="store_true", help="also score the vendor-agnostic path")
    t.add_argument("--out", type=Path)
    t.add_argument("--image", action="append", help="only this image (repeatable); results merge")

    cp = sub.add_parser("capsule", help="seal a replay capsule: byte ranges of every clip, signed (Ed25519)")
    cp.add_argument("case", type=Path)
    rp = sub.add_parser("replay", help="rebuild every clip from the evidence images using only a capsule")
    rp.add_argument("capsule", type=Path, help="the capsule folder (case/capsule)")
    rp.add_argument("--image", action="append", default=[], metavar="EID=PATH",
                    help="where an evidence image is on this machine, e.g. E1=D:\\evidence\\dvr.img (repeatable)")

    kz = sub.add_parser("kasauti", help="Recorder validation: score a real recorder image against a handwritten event log")
    kz.add_argument("image")
    kz.add_argument("--events", required=True, type=Path, help="event log CSV (sarvadristhi kasauti-template prints one)")
    kz.add_argument("--out", required=True, type=Path)
    kz.add_argument("--make", default="")
    kz.add_argument("--model", default="")
    kz.add_argument("--serial", default="")
    kz.add_argument("--firmware", default="")
    kz.add_argument("--physical", action="store_true", help="the image comes from a physical recorder")
    kz.add_argument("--recorder-offset", type=float, default=0.0, help="recorder clock minus reference clock, seconds")
    kz.add_argument("--examiner", default="")
    sub.add_parser("kasauti-template", help="print the Recorder validation event-log template")

    x = ap.parse_args()
    if x.cmd == "kasauti-template":
        from .kasauti import TEMPLATE
        print(TEMPLATE, end="")
        return
    if x.cmd == "kasauti":
        from .kasauti import run
        r = run(x.image, x.events, x.out, {"make": x.make, "model": x.model, "serial": x.serial, "firmware": x.firmware},
                x.physical, x.recorder_offset, x.examiner)
        print((x.out / "kasauti.md").read_text(encoding="utf-8"))
        return
    if x.cmd == "capsule":
        from .capsule import build
        s = build(x.case)
        print(json.dumps(s, indent=2))
        sys.exit(0 if s["built_ok"] == s["clips"] and not s["problems"] else 1)
    if x.cmd == "replay":
        from .capsule import replay
        r = replay(x.capsule, images=dict(i.split("=", 1) for i in x.image))
        print(f"signature: {'valid' if r['signature_ok'] else 'INVALID'} (key {r['key_fingerprint']})")
        for i in r["items"]:
            print(f"  {i['id']}: {'image matches seizure hash' if i.get('sha256_ok') else 'IMAGE MISSING OR CHANGED'}  {i.get('path')}")
        for k in r["clips"]:
            print(f"  {'OK ' if k['ok'] else 'BAD'} {k['id']}  {k['ranges']} ranges  {k.get('why', '')}")
        print(f"{r['reproduced']}/{r['total']} clips reproduced in {r['seconds']} s")
        sys.exit(0 if r["signature_ok"] and r["reproduced"] == r["total"] else 1)
    if x.cmd == "analyze":
        from .pipeline import run
        c = run(x.source, x.case, x.case_id, x.examiner, acquire_to=x.acquire_to, acquire_format=x.acquire_format,
                mode=x.mode, export=not x.no_export, analytics=not x.no_analytics, overlay=not x.no_overlay,
                pdf=not x.no_pdf, ai=x.ai)
        print(f"\n{c['detection']['family']}: {c['recovery']['frames']} frames, {len(c['clips'])} clips, "
              f"{len(c['findings'])} findings. Report: {x.case / c['report'].get('pdf', c['report']['html'])}")
    elif x.cmd == "detect":
        from .detect import detect
        from .source import Source
        with Source(x.source) as s:
            print(json.dumps(detect(s), indent=2))
    elif x.cmd == "acquire":
        from .acquire import acquire
        from .custody import Ledger
        if x.format == "e01":
            from .ewf import acquire_e01
            r = acquire_e01(x.source, x.image, Ledger(x.case / "custody.jsonl"))
        else:
            r = acquire(x.source, x.image, Ledger(x.case / "custody.jsonl"),
                        progress=lambda p, n: print(f"\r{p * 100 // max(1, n)}%", end="", flush=True))
        print("\n" + json.dumps({k: v for k, v in r.items() if k != "unreadable_sectors"}, indent=2))
    elif x.cmd == "live":
        from .custody import Ledger
        from .live import acquire_live
        r = acquire_live(x.host, x.user, x.password, x.case / "live", Ledger(x.case / "custody.jsonl"),
                         vendor=x.vendor, channels=[int(c) for c in x.channels.split(",")],
                         start=x.start, end=x.end, port=x.port)
        (x.case / "live.json").write_text(json.dumps(r, indent=2))
        print(json.dumps({k: r.get(k) for k in ("device", "clock", "vendor")}, indent=2))
        for d in r["downloads"]:
            print(f"  ch{d['channel']}: {d['bytes']:,} bytes  sha256 {d['sha256']}  -> {d['file']}")
            if x.analyze:
                from .pipeline import run
                run(d["file"], x.case / f"analysis_ch{d['channel']:02d}", f"LIVE-CH{d['channel']}", "live")
    elif x.cmd == "ui":
        from .ui import serve
        serve(x.cases, x.port, not x.no_browser)
    elif x.cmd == "verify-custody":
        from .custody import Ledger
        ok, msg = Ledger(x.case / "custody.jsonl").verify()
        print(("OK: " if ok else "TAMPERED: ") + msg)
        sys.exit(0 if ok else 1)
    elif x.cmd == "validate":
        from .validate import validate
        validate(x.testkit_out, universal=x.universal, out=x.out, only=x.image)


if __name__ == "__main__":
    main()
