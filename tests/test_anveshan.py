"""Tests for Person search's person grouping and face ownership: tracker breaks are joined, identity
switches and people walking through the same spot are not, and a neighbour's face never counts.

Run: python -m pytest -q tests/test_anveshan.py"""
import numpy as np

from sarvadristhi import anveshan

CAM = "DVR-B · CAM-01"


def _track(tid, start_s, end_s, entry, best=None, exit_=None, cam=CAM):
    best, exit_ = best or entry, exit_ or entry
    return {"id": tid, "channel_name": cam, "frame_size": [1280, 720], "abs_start_ms": int(start_s * 1000),
            "abs_end_ms": int(end_s * 1000), "entry_box": entry, "best_box": best, "exit_box": exit_}


def _sig(colour_seed, face=None):
    rng = np.random.default_rng(colour_seed)
    h = rng.random(192).astype(np.float32)
    return {"upper": h, "lower": h, "upper_colour": "grey", "lower_colour": "black", "face": face, "face_px": 20}


SEAT = [760, 225, 896, 451]


def test_tracker_break_on_same_seat_is_joined():
    ts = [_track("A", 0, 20, SEAT), _track("B", 23, 60, [758, 226, 897, 452])]
    chain, links = anveshan._chains(ts, {"A": _sig(1), "B": _sig(1)})
    assert chain["A"] == chain["B"] and len(links) == 1


def test_different_clothes_or_camera_or_same_time_not_joined():
    ts = [_track("A", 0, 20, SEAT), _track("B", 23, 60, SEAT)]
    assert anveshan._chains(ts, {"A": _sig(1), "B": _sig(2)})[0]["B"] == "B"            # other clothes
    ts = [_track("A", 0, 20, SEAT), _track("B", 23, 60, SEAT, cam="DVR-A · CAM-02")]
    assert anveshan._chains(ts, {"A": _sig(1), "B": _sig(1)})[0]["B"] == "B"            # other camera
    ts = [_track("A", 0, 40, SEAT), _track("B", 20, 60, SEAT)]
    assert anveshan._chains(ts, {"A": _sig(1), "B": _sig(1)})[0]["B"] == "B"            # same time


def test_identity_switch_track_is_never_joined():
    # B enters as the seated person but its best box is someone tall walking past: the tracker switched
    ts = [_track("A", 0, 20, SEAT), _track("B", 22, 30, SEAT, best=[1006, 113, 1087, 340])]
    assert anveshan._chains(ts, {"A": _sig(1), "B": _sig(1)})[0]["B"] == "B"


def test_faces_that_disagree_block_a_join():
    f1, f2 = np.zeros(128, np.float32), np.zeros(128, np.float32)
    f1[0], f2[1] = 1, 1                                                                     # cosine 0
    ts = [_track("A", 0, 20, SEAT), _track("B", 23, 60, SEAT)]
    assert anveshan._chains(ts, {"A": _sig(1, f1), "B": _sig(1, f2)})[0]["B"] == "B"


def test_each_track_gets_one_successor_nearest_first():
    ts = [_track("A", 0, 10, SEAT), _track("B", 12, 20, SEAT), _track("C", 60, 70, SEAT)]
    chain, links = anveshan._chains(ts, {k: _sig(1) for k in "ABC"})
    assert {(l["from"], l["to"]) for l in links} == {("A", "B"), ("B", "C")}
    assert chain["A"] == chain["B"] == chain["C"]


def test_only_the_persons_own_face_counts():
    img = np.zeros((200, 100, 3), np.uint8)

    def faces(boxes):                                  # stands in for perception.Faces on the 3x image
        return lambda big: [{"box": [v * 3 for v in b], "conf": c, "embedding": np.ones(128, np.float32)}
                            for b, c in boxes]
    assert anveshan._own_face(img, faces([([40, 20, 60, 40], 0.9)])) is not None          # head, centred, top
    assert anveshan._own_face(img, faces([([40, 170, 60, 190], 0.9)])) is None            # bottom: a neighbour
    assert anveshan._own_face(img, faces([([0, 20, 15, 40], 0.9)])) is None               # edge: a neighbour
    assert anveshan._own_face(img, faces([([45, 20, 55, 30], 0.9)])) is None              # 10 px: too small
    assert anveshan._own_face(img, faces([([40, 20, 60, 40], 0.6)])) is None              # unsure detection
