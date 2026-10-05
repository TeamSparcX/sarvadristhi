"""Tests for H.265 support, the replay capsule, Ed25519 sealing and the workbench analyses
(format hypotheses, reconciliation, ask-the-case). Builds its own short H.265 evidence set.

Run: python -m pytest -q tests/test_workbench.py"""
import csv
import json
import shutil
from pathlib import Path

import pytest

from sarvadristhi import capsule, ed25519, workbench
from sarvadristhi.formats import codec
from sarvadristhi.recover import recover
from sarvadristhi.source import Source
from sarvadristhi.validate import _score

needs_tools = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("tesseract")),
                                 reason="ffmpeg and tesseract required")


def test_ed25519_rfc8032_vectors():
    sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pk = ed25519.public_key(sk)
    assert pk.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = ed25519.sign(sk, b"")
    assert sig.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ed25519.verify(pk, b"", sig) and not ed25519.verify(pk, b"x", sig)
    sk2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    assert ed25519.sign(sk2, b"\x72").hex() == ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                                                "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")


def test_codec_sniff_never_mistakes_h264_slices_for_hevc():
    h264 = b"\x00\x00\x00\x01\x67\x4d\x40\x1e" + b"\x00\x00\x01\x68\xee\x3c\x80" + b"\x00\x00\x01\x41\x9a" * 50
    hevc = b"\x00" * 300 + b"\x00\x00\x00\x01\x40\x01\x0c" + b"\x00\x00\x01\x42\x01\x01" + b"\x00\x00\x01\x44\x01\xc1"
    assert codec.sniff(h264) == codec.H264
    assert codec.sniff(hevc) == codec.HEVC                    # leading zeros must not hide the stream
    assert not codec.has_keyframe(b"\x00\x00\x01\x41\x9a", codec.H264)
    assert codec.has_keyframe(b"\x00\x00\x01\x26\x01\xaf", codec.HEVC)   # IDR_W_RADL


@pytest.fixture(scope="module")
def h265kit(tmp_path_factory):
    from evidence_builder.h265 import build_h265
    out = tmp_path_factory.mktemp("h265kit")
    build_h265(out, seconds=40)
    return out


def _rows(kit, name):
    return list(csv.DictReader((kit / "ground_truth" / f"{name}.csv").open()))


@needs_tools
@pytest.mark.parametrize("name,family", [("dahua_h265_intact", "dahua-dhav"), ("dahua_h265_overwritten", "dahua-dhav"),
                                         ("hikvision_h265_intact", "hikvision"), ("hikvision_h265_formatted", "hikvision")])
def test_h265_native_recovery_is_exact(h265kit, name, family):
    with Source(str(h265kit / "images" / f"{name}.img")) as s:
        det, rec = recover(s, mode="auto")
    assert det["family"] == family and rec.metadata.get("codec") == "hevc"
    sc = _score(_rows(h265kit, name), rec.frames, universal=False)
    assert sc["recovery_rate"] == 1.0 and sc["false_positives"] == 0
    assert sc["temporal_accuracy"] == 1.0 and sc["camera_accuracy"] == 1.0
    if "index_level_accuracy" in sc:
        assert sc["index_level_accuracy"] == 1.0


@needs_tools
def test_h265_universal_recovers_every_frame(h265kit):
    with Source(str(h265kit / "images" / "dahua_h265_intact.img")) as s:
        _, rec = recover(s, mode="universal")
    sc = _score(_rows(h265kit, "dahua_h265_intact"), rec.frames, universal=True)
    assert sc["recovery_rate"] == 1.0 and sc["false_positives"] == 0


def test_honeywell_record_known_answer_from_paper():
    """Yoon & Hwang 2026, section 5.4.6: timestamp 0x000644865C1BCEE6 = 2025-11-26 21:48:41.896."""
    import struct
    from datetime import datetime, timezone
    from sarvadristhi.formats import honeywell
    body = b"\x00\x00\x00\x01\x65" + b"\x88" * 40
    rec = b"\x82\x80\x01\x00" + struct.pack("<HHIQ", 1920, 1080, len(body), 0x000644865C1BCEE6) + body
    buf = b"\x00" * 64 + rec + b"\x00" * 20
    r = list(honeywell.scan(buf))
    assert len(r) == 1 and r[0].keyframe and (r[0].width, r[0].height) == (1920, 1080)
    t = datetime.fromtimestamp(r[0].time_ms / 1000, timezone.utc)
    assert t.strftime("%Y-%m-%d %H:%M:%S.%f")[:23] == "2025-11-26 21:48:41.896"
    torn = rec[:-10] + b"\x11" * 30                      # length overruns into foreign bytes
    rejects = []
    assert list(honeywell.scan(b"\x00" * 64 + torn, rejects=rejects)) == [] and rejects == [64]


@needs_tools
def test_integrity_flags_splice_and_cut(tmp_path):
    import subprocess
    from sarvadristhi import integrity
    enc = ["-c:v", "libx264", "-threads", "1", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p"]
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "4", *enc, str(a)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=352x288:rate=10", "-t", "4", *enc, str(b)], check=True)
    clean = integrity.check(a)
    assert clean["ok"] and clean["frames"] == 40 and clean["keyframes"] == 4
    lst = tmp_path / "l.txt"
    lst.write_text(f"file '{a.as_posix()}'\nfile '{b.as_posix()}'\n")
    spliced = tmp_path / "spliced.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    "-bsf:v", "h264_mp4toannexb", str(spliced)], check=True)
    r = integrity.check(spliced)
    assert r["sps_changes"] and r["sps_changes"][0]["before_frame"] == 40
    cut = tmp_path / "cut.ts"                         # frames 1.0-2.0 s removed, original timestamps kept
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(a), "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                    "-copyts", "-f", "mpegts", "-ss", "0", "-to", "1", str(tmp_path / "p1.ts")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-copyts", "-ss", "2", "-i", str(a), "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                    "-f", "mpegts", str(tmp_path / "p2.ts")], check=True)
    cut.write_bytes((tmp_path / "p1.ts").read_bytes() + (tmp_path / "p2.ts").read_bytes())
    g = integrity.check(cut)
    assert g["timestamp_gaps"] and g["timestamp_gaps"][0]["gap_s"] >= 0.9
    assert {f["kind"] for f in integrity.findings("x", r)} == {"export-structure"}


@pytest.fixture(scope="module")
def hwkit(tmp_path_factory):
    from evidence_builder.honeywell import build
    out = tmp_path_factory.mktemp("hwkit")
    build(out, seconds=40)
    return out


@needs_tools
@pytest.mark.parametrize("name", ["honeywell_intact", "honeywell_overwritten"])
def test_honeywell_native_recovery(hwkit, name):
    with Source(str(hwkit / "images" / f"{name}.img")) as s:
        det, rec = recover(s, mode="auto")
    assert det["family"] == "honeywell"
    sc = _score(_rows(hwkit, name), rec.frames, universal=False)
    assert sc["recovery_rate"] == 1.0 and sc["false_positives"] == 0 and sc["temporal_accuracy"] == 1.0
    assert sc["camera_accuracy"] >= 0.95                 # camera from overlay OCR (channel list not parsed)
    if name.endswith("overwritten"):
        assert any(f.kind == "torn-frames" for f in rec.findings)


@pytest.fixture(scope="module")
def labelled_case(h265kit, tmp_path_factory):
    from sarvadristhi.pipeline import run
    from evidence_builder.labels import relabel
    d = tmp_path_factory.mktemp("labelled")
    img = d / "cpplus_label_hikvision_h265.img"
    relabel(h265kit / "images" / "hikvision_h265_formatted.img", img)
    case = d / "case"
    run(str(img), case, "LBL-1", "pytest", analytics=False, pdf=False, log=lambda m: None)
    return case


@needs_tools
def test_label_conflict_is_reported_and_structure_decides(labelled_case):
    h = workbench.hypotheses(labelled_case)[0]
    assert h["family"] == "hikvision" and h["state"] == "accepted"
    assert [b["brand"] for b in h["label_conflicts"]] == ["CP Plus"]
    win = next(x for x in h["hypotheses"] if x["selected"])
    assert all(c["ok"] for c in win["checks"] if c["ok"] is not None)


@needs_tools
def test_h265_clips_export_and_replay_capsule(labelled_case):
    c = json.loads((labelled_case / "case.json").read_text(encoding="utf-8"))
    vids = [k for k in c["clips"] if k.get("mp4_file")]
    assert vids and all(k.get("codec") == "hevc" for k in vids)
    s = capsule.build(labelled_case, log=lambda m: None)
    assert s["clips"] == len([k for k in c["clips"] if k.get("raw_sha256")]) and s["built_ok"] == s["clips"]
    assert not s["problems"]
    r = capsule.replay(labelled_case / "capsule", log=lambda m: None)
    assert r["signature_ok"] and r["reproduced"] == r["total"] and all(i["sha256_ok"] for i in r["items"])
    victim = r["clips"][0]["id"]
    t = capsule.replay(labelled_case / "capsule", tamper=victim, log=lambda m: None)
    assert [k["id"] for k in t["clips"] if not k["ok"]] == [victim]
    body = json.loads((labelled_case / "capsule" / "capsule.json").read_text(encoding="utf-8"))
    body["clips"][0]["ranges"][0][0] += 1                          # edit the capsule itself
    (labelled_case / "capsule" / "capsule.json").write_text(json.dumps(body), encoding="utf-8")
    assert capsule.load(labelled_case / "capsule")[1] is False      # signature no longer verifies


@needs_tools
def test_reconcile_and_ask(labelled_case):
    r = workbench.reconcile(labelled_case)
    assert r["rows"] and all(set(row["cells"]) == {"index", "disk", "log", "overlay", "export"} for row in r["rows"])
    assert any(row["cells"]["log"]["rel"] == "discrepant" for row in r["rows"])     # format event vs older frames
    a = workbench.ask(labelled_case, "Who is the man at the gate?")
    assert a["refused"] and not a["citations"]
    a = workbench.ask(labelled_case, "Was the recorder formatted?")
    assert not a["refused"] and any(x["id"] == "format-event" for x in a["citations"])
    a = workbench.ask(labelled_case, "tell me about the weather")
    assert a["refused"]


@needs_tools
def test_examiner_records_frame_xray_and_report(labelled_case):
    from sarvadristhi import examiner, frame_xray
    from sarvadristhi.custody import Ledger
    from sarvadristhi.ui import regenerate_report
    c = json.loads((labelled_case / "case.json").read_text(encoding="utf-8"))
    k = next(x for x in c["clips"] if x.get("mp4_file"))
    n = examiner.add_note(labelled_case, k["id"], 1.0, 4.0, "subject enters from the left", "pytest")
    assert n["frame"] and (labelled_case / n["frame"]).exists() and n["recorder_from_ms"] == k["start_ms"] + 1000
    with pytest.raises(ValueError):
        examiner.add_note(labelled_case, k["id"], 1.0, 2.0, "  ", "pytest")              # empty finding refused
    o = examiner.set_offset(labelled_case, "E1", -12.5, "recorder 12.5 s ahead of NTP at seizure", "pytest")
    assert examiner.offsets(labelled_case)["current"]["E1"]["offset_s"] == -12.5 and o["reason"]
    s = examiner.export_segment(labelled_case, k["id"], 2.0, 6.0, "excerpt", "pytest")
    assert s["frames"] > 0 and (labelled_case / s["file"]).exists() and s["source_mp4_sha256"] == k["mp4_sha256"]
    acts = [e["action"] for e in Ledger(labelled_case / "custody.jsonl").entries()]
    assert {"examiner-finding", "clock-offset", "segment-export"} <= set(acts)
    assert Ledger(labelled_case / "custody.jsonl").verify()[0]
    x = frame_xray.frames(labelled_case, k["id"])                      # every frame of the clip mapped to its bytes
    assert x["mode"] == "frames" and len(x["frames"]) == k["frames"]
    h = frame_xray.header(labelled_case, "E1", x["frames"][0][1])
    names = [f["name"] for f in h["fields"]]
    assert "MPEG-PS pack header" in names and "program stream map" in names and h["fields"][-1]["value"].startswith("type")
    r = regenerate_report(labelled_case)
    assert r["ok"] and set(r["hashes"]) >= {"html"}
    html = (labelled_case / "report.html").read_text(encoding="utf-8")
    assert "Examiner findings" in html and "Recorder clock corrections" in html and "Exported segments" in html and "report_frames/" in html


def test_frame_xray_decodes_vendor_headers():
    import struct
    from sarvadristhi import frame_xray
    hw = b"\x82\x80\x01\x00" + struct.pack("<HHIQ", 1920, 1080, 100, 0x000644865C1BCEE6) + b"\x00\x00\x00\x01\x67"
    f = {x["name"]: x["value"] for x in frame_xray.decode(hw, "honeywell")}
    assert f["resolution"] == "1920 x 1080" and f["recorder time (µs)"] == "2025-11-26 21:48:41.896"
    assert f["first NAL (H.264)"].startswith("type 7")


@needs_tools
def test_recorder_validation_rehearsal(h265kit, labelled_case, tmp_path):
    from sarvadristhi import recorder_validation
    ev = recorder_validation.events_from_ground_truth(h265kit / "ground_truth" / "hikvision_h265_formatted.csv", tmp_path / "log.csv")
    s = recorder_validation.score(labelled_case, recorder_validation.read_log(ev))
    recs = [r for r in s["results"] if r["action"] == "recording"]
    assert recs and all(r["verdict"] == "recovered" for r in recs) and s["mean_coverage"] >= 0.99
    caps = recorder_validation.promote(workbench.capabilities(), tmp_path / "registry")
    assert all(o["cells"]["recover"] != "real" for o in caps["oems"])      # nothing is "real" without a physical run
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "r.json").write_text(json.dumps({"physical_recorder": True, "family": "hikvision", "mean_coverage": 0.95}))
    caps = recorder_validation.promote(workbench.capabilities(), tmp_path / "registry")
    assert next(o for o in caps["oems"] if o["oem"] == "Hikvision")["cells"]["recover"] == "real"
