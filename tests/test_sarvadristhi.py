"""End-to-end tests against a freshly built (short) dvr-pramanshala evidence set.

Run: python -m pytest -q tests/        (needs ffmpeg with libx264, tesseract)"""
import csv
import json
import shutil
from pathlib import Path

import pytest

from sarvadristhi import clockdrift
from sarvadristhi.acquire import acquire, hash_file
from sarvadristhi.custody import Ledger
from sarvadristhi.formats import h264
from sarvadristhi.models import EvidenceLevel
from sarvadristhi.recover import recover
from sarvadristhi.source import Source
from sarvadristhi.validate import _score

needs_tools = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("tesseract")),
                                 reason="ffmpeg and tesseract required")
SECONDS = 45


@pytest.fixture(scope="session")
def kit(tmp_path_factory):
    from pramanshala.build import build
    out = tmp_path_factory.mktemp("kit")
    build(out, seconds=SECONDS)
    return out


def rows(kit, name):
    return list(csv.DictReader((kit / "ground_truth" / f"{name}.csv").open()))


def run(kit, name, mode="auto"):
    with Source(str(kit / "images" / f"{name}.img")) as s:
        return recover(s, mode=mode)


@needs_tools
@pytest.mark.parametrize("name,family", [("dahua_intact", "dahua-dhav"), ("dahua_overwritten", "dahua-dhav"),
                                         ("hikvision_intact", "hikvision"), ("hikvision_formatted", "hikvision"),
                                         ("hikvision_overwritten", "hikvision")])
def test_native_recovery_is_exact(kit, name, family):
    det, rec = run(kit, name)
    assert det["family"] == family
    s = _score(rows(kit, name), rec.frames, universal=False)
    assert s["recovery_rate"] == 1.0
    assert s["false_positives"] == 0
    assert s["temporal_accuracy"] == 1.0
    assert s["camera_accuracy"] == 1.0
    if "index_level_accuracy" in s:
        assert s["index_level_accuracy"] == 1.0


@needs_tools
def test_evidence_levels_follow_the_index(kit):
    _, intact = run(kit, "hikvision_intact")
    _, formatted = run(kit, "hikvision_formatted")
    assert all(f.level == EvidenceLevel.INDEX_CONFIRMED for f in intact.frames)
    assert all(f.level == EvidenceLevel.CARVED_TIMESTAMPED for f in formatted.frames)


@needs_tools
def test_format_is_detected_from_logs_and_index(kit):
    _, rec = run(kit, "hikvision_formatted")
    kinds = {f.kind for f in rec.findings}
    assert {"format-event", "index-reset", "empty-index"} <= kinds
    _, intact = run(kit, "hikvision_intact")
    assert not {"format-event", "index-reset"} & {f.kind for f in intact.findings}


@needs_tools
def test_torn_frame_at_overwrite_boundary(kit):
    _, rec = run(kit, "dahua_overwritten")
    assert any(f.kind == "torn-frames" for f in rec.findings)


@needs_tools
def test_universal_path_attributes_camera_and_time_from_overlay(kit):
    _, rec = run(kit, "dahua_intact", mode="universal")
    s = _score(rows(kit, "dahua_intact"), rec.frames, universal=True)
    assert s["recovery_rate"] == 1.0 and s["false_positives"] == 0
    assert s["camera_accuracy"] >= 0.99
    assert s["temporal_accuracy"] >= 0.98


@needs_tools
def test_clock_drift_and_clock_change(kit):
    m = json.loads((kit / "manifest.json").read_text())
    with Source(str(kit / "images" / "hikvision_intact.img")) as s:
        _, rec = recover(s)
        res, findings = clockdrift.measure(s.buf, rec.frames)
    want = {c["channel"]: c["drift_s"] for c in m["cameras"]}
    for ch in (2, 3):
        assert res[ch]["drift_s"] == want[ch]
    changes = [f for f in findings if f.kind == "clock-change"]
    assert [f.detail["channel"] for f in changes] == [1]          # and no false alarms
    step = changes[0].detail["steps"][0]
    assert step["before_s"] == 0 and step["after_s"] == -900


def test_custody_ledger_detects_tampering(tmp_path):
    led = Ledger(tmp_path / "custody.jsonl")
    for k in range(3):
        led.append("step", n=k)
    assert led.verify()[0]
    lines = led.path.read_text().splitlines()
    lines[1] = lines[1].replace('"n": 1', '"n": 9')
    led.path.write_text("\n".join(lines) + "\n")
    ok, msg = led.verify()
    assert not ok and "entry 1" in msg


def test_acquisition_hashes_match(tmp_path):
    src = tmp_path / "disk.bin"
    src.write_bytes(bytes(range(256)) * 40000)
    r = acquire(str(src), tmp_path / "image.raw", Ledger(tmp_path / "c.jsonl"))
    assert r["verified"] and r["image_hash"] == hash_file(src)
    with pytest.raises(FileExistsError):
        acquire(str(src), tmp_path / "image.raw", Ledger(tmp_path / "c.jsonl"))


def test_h264_carver_ignores_noise():
    import random
    rnd = random.Random(26150)
    noise = bytes(rnd.getrandbits(8) for _ in range(2_000_000))
    assert len(list(h264.carve_aus(noise, 0, len(noise)))) <= 2


@needs_tools
def test_full_case_run_produces_verified_report(kit, tmp_path):
    from sarvadristhi.pipeline import run as case_run
    c = case_run(str(kit / "images" / "dahua_overwritten.img"), tmp_path / "case", "T-1", "pytest")
    assert c["evidence"]["unchanged"] and c["custody"]["ok"]
    assert (tmp_path / "case" / "report.html").exists()
    assert "pdf" in c["report"] or "pdf_error" in c["report"]
    for k in c["clips"]:   # playable if it has a keyframe; otherwise the reason is recorded
        assert k.get("mp4_sha256") if k["keyframes"] else "no keyframe" in k.get("mp4_error", "")
    assert any(e["channel"] == 1 for e in c["events"])          # subject crossing CAM-01


def test_label_consensus_on_real_ocr_readings():
    """Readings captured from the OCR of a noisy overlay (tests/fixtures_label_readings.json)."""
    from sarvadristhi.ocr import consensus_label, parse_label
    readings = json.loads((Path(__file__).parent / "fixtures_label_readings.json").read_text())
    assert [consensus_label(r) for r in readings] == ["CAM-01 MAIN GATE", "CAM-02 PARKING", "CAM-03 CORRIDOR"]
    assert parse_label("CHO ERE")[1] is None          # OCR junk is not a camera number
    assert parse_label("gCAM-Q3 CORRIDOR")[1] == 3     # Q read for 0, junk prefix dropped


@pytest.fixture
def mock_nvr(kit):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from mock_nvr import MockNVR
    return lambda mfr: MockNVR(kit / "exports", manufacturer=mfr)


@needs_tools
def test_live_dahua_pull_is_time_filtered_and_logged(mock_nvr, tmp_path):
    from sarvadristhi.live import Onvif, acquire_live
    with mock_nvr("Dahua") as nvr:
        led = Ledger(tmp_path / "c.jsonl")
        r = acquire_live("127.0.0.1", "admin", "secret1234", tmp_path / "live", led, channels=(1, 2),
                         start="2026-09-28 21:14:10", end="2026-09-28 21:14:29", port=nvr.port)
        assert r["vendor"] == "dahua" and r["device"]["SerialNumber"] == "SN-MOCK-0001"
        assert len(r["recordings"]) == 3 and abs(r["clock"]["device_minus_examiner_s"]) <= 2
        with pytest.raises(RuntimeError):
            Onvif("127.0.0.1", "admin", "wrong", nvr.port).device_information()
    for d in r["downloads"]:
        with Source(d["file"]) as s:
            det, rec = recover(s)
        secs = [f.time_ms // 1000 - 1790630040 for f in rec.frames]
        assert det["family"] == "dahua-dhav" and {f.channel for f in rec.frames} == {d["channel"]}
        assert min(secs) >= 10 and max(secs) <= 29
        assert hash_file(Path(d["file"]))["sha256"] == d["sha256"]
    assert led.verify()[0]


@needs_tools
def test_setugrahan_job_acquires_analyses_and_never_stores_the_password(mock_nvr, tmp_path, monkeypatch):
    from sarvadristhi import jobrun
    case = tmp_path / "LIVE-1"
    form = {"kind": "live", "case_id": "LIVE-1", "examiner": "pytest", "case_dir": str(case), "ai": False,
            "live": {"host": "127.0.0.1", "user": "admin", "vendor": "dahua", "channels": [1, 2],
                     "start": "2026-09-28 21:14:10", "end": "2026-09-28 21:14:29"}}
    with mock_nvr("Dahua") as nvr:
        form["live"]["port"] = nvr.port
        monkeypatch.setenv("SARVADRISTHI_LIVE_PASSWORD", "secret1234")
        assert jobrun.live(form, lambda m: None) == 0
    c = json.loads((case / "case.json").read_text(encoding="utf-8"))
    assert len(c["evidence_items"]) == 2 and c["clips"]
    stored = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in case.rglob("*") if p.suffix in (".json", ".jsonl"))
    assert "secret1234" not in stored
    assert Ledger(case / "custody.jsonl").verify()[0]


@needs_tools
def test_live_hikvision_pull(mock_nvr, tmp_path):
    from sarvadristhi.live import acquire_live
    with mock_nvr("HIKVISION") as nvr:
        r = acquire_live("127.0.0.1", "admin", "secret1234", tmp_path / "live", Ledger(tmp_path / "c.jsonl"),
                         channels=(2,), start="2026-09-28 21:14:00", end="2026-09-28 21:14:44", port=nvr.port)
    assert r["vendor"] == "hikvision" and len(r["downloads"]) == 1
    with Source(r["downloads"][0]["file"]) as s:
        det, rec = recover(s)
    assert len(rec.frames) == SECONDS * 10 and {f.channel for f in rec.frames} == {2}


@needs_tools
def test_sensor_fingerprint_attribution_and_control(kit):
    """Three overlay-less cameras film an identical scene; only a sensor fingerprint differs.
    The control image has no fingerprint, so attribution must be refused, not guessed."""
    _, rec = run(kit, "dahua_no_overlay", mode="universal")
    s = _score(rows(kit, "dahua_no_overlay"), rec.frames, universal=True)
    assert rec.metadata["attribution"]["sensor_fingerprint"]["clusters"] == 3
    assert s["attributed"] >= 0.95 * s["recovered"] and s["camera_accuracy"] >= 0.98
    _, ctl = run(kit, "dahua_no_overlay_control", mode="universal")
    assert ctl.metadata["attribution"]["sensor_fingerprint"]["clusters"] == 0
    assert all(f.channel is None for f in ctl.frames)


@needs_tools
def test_ui_runs_a_case_and_serves_clips(kit, tmp_path):
    import threading
    import time
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer
    from sarvadristhi.ui import App, handler
    app = App(tmp_path / "cases")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        form = urllib.parse.urlencode({"source": str(kit / "images" / "dahua_intact.img"),
                                       "case_id": "UI-1", "examiner": "pytest"}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + "/analyze", form))
        jid = r.url.rsplit("/", 1)[1]
        for _ in range(240):
            if app.jobs[jid]["state"] != "running":
                break
            time.sleep(0.5)
        assert app.jobs[jid]["state"] == "done", app.jobs[jid]["log"][-3:]
        assert b"UI-1" in urllib.request.urlopen(base + "/").read()
        page = urllib.request.urlopen(base + "/case/UI-1").read()
        assert b"<video" in page and b"Findings" in page
        clip = json.loads((tmp_path / "cases" / "UI-1" / "case.json").read_text())["clips"][0]["mp4_file"]
        part = urllib.request.urlopen(urllib.request.Request(base + f"/case/UI-1/file/clips/{clip}",
                                                             headers={"Range": "bytes=0-99"}))
        assert part.status == 206 and len(part.read()) == 100
        assert b"intact" in urllib.request.urlopen(base + "/case/UI-1/verify").read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/case/UI-1/file/../../../etc/passwd")
    finally:
        srv.shutdown()


@pytest.mark.skipif(not shutil.which("ewfacquire"), reason="ewf-tools required")
def test_e01_round_trip(kit, tmp_path):
    from sarvadristhi import ewf
    src = kit / "images" / "dahua_intact.img"
    led = Ledger(tmp_path / "c.jsonl")
    r = ewf.acquire_e01(str(src), tmp_path / "evidence", led, case_id="E-1", examiner="pytest")
    assert r["ewfverify_ok"] and r["sha256"] == hash_file(src)["sha256"]
    x = ewf.expand(Path(r["image"]), tmp_path / "work.raw", led)
    assert x["hashes_match"] and x["computed"]["sha256"] == hash_file(src)["sha256"]
    with Source(str(tmp_path / "work.raw")) as s:
        det, rec = recover(s)
    assert det["family"] == "dahua-dhav" and len(rec.frames) == len(rows(kit, "dahua_intact"))
    assert led.verify()[0]
