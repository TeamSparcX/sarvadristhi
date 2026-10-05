"""Universal recovery for any recorder (Tier 3): container-agnostic H.264 / H.265 carving, then
camera and time attribution from the burned-in overlay (OCR), after the CARVE method
(Giri, Yoon & Hwang, DFRWS APAC 2026). PRNU clustering for overlay-less footage is a
planned second attribution path."""
import re
import statistics
from collections import defaultdict

from .. import ocr
from ..formats import codec, h264, mpegps
from ..models import EvidenceLevel, Frame, Recovery
from .base import Plugin

PACK = b"\x00\x00\x01\xBA"
CONFUSABLE = {"0": "689", "6": "058", "8": "0369", "9": "08", "1": "74", "7": "1", "4": "1",
              "3": "8", "5": "6"}


def es_bytes(buf, f: Frame) -> bytes:
    return b"".join(bytes(buf[o:o + n]) for o, n in f.es)


def gaps(size: int, used: list[tuple[int, int]], min_len: int = 4096):
    pos = 0
    for a, n in sorted(used):
        if a - pos >= min_len:
            yield pos, a
        pos = max(pos, a + n)
    if size - pos >= min_len:
        yield pos, size


class GenericPlugin(Plugin):
    name = "universal"
    family = "unknown"

    def detect(self, src):
        packs, i, end = 0, 0, min(src.size, 256 * 1024 * 1024)
        while packs < 10000:
            i = src.buf.find(PACK, i, end)
            if i < 0:
                break
            packs, i = packs + 1, i + 4
        return (0.3 if packs else 0.1), {"ps_pack_headers_first_256MiB": packs}

    def carve(self, src, start=0, end=None) -> list[Frame]:
        end = src.size if end is None else end
        ps_codec = mpegps.sniff_codec(src.buf, start, end)
        frames = [Frame(f.offset, f.length, tuple(f.es), f.keyframe(src.buf, ps_codec), "carved-ps",
                        EvidenceLevel.CONTENT_ONLY, pts_90k=f.pts)
                  for f in mpegps.walk(src.buf, start, end)]
        used = [(f.offset, f.length) for f in frames]
        for a, b in gaps(end, [(o, n) for o, n in used if o >= start], 64):
            if a < start:
                a = start
            hevc = codec.sniff(src.buf, start=a, end=b) == codec.HEVC
            carver = codec.carve_aus_hevc if hevc else h264.carve_aus
            for au in carver(src.buf, a, b):
                frames.append(Frame(au.offset, au.length, ((au.offset, au.length),), au.keyframe,
                                    "carved-h265" if hevc else "carved-h264", EvidenceLevel.CONTENT_ONLY))
        frames.sort(key=lambda f: f.offset)
        return frames

    def attribute(self, src, frames: list[Frame], fps_hint: float | None = None) -> dict:
        """Group frames into GOPs (disk order) and read each keyframe's overlay, then
        (1) cluster label readings by text similarity, so OCR digit slips do not create
            phantom cameras; (2) assign unread labels by template match;
        (3) keep only clock readings consistent with their neighbours and interpolate the
            rest between trusted readings of the same camera."""
        gops, cur = [], []
        for f in frames:
            if f.keyframe and cur:
                gops.append(cur)
                cur = []
            cur.append(f)
        if cur:
            gops.append(cur)
        gops = [g for g in gops if g[0].keyframe]
        if not gops or not ocr.available():
            return {"gops": len(gops), "ocr": "unavailable" if gops else "no keyframes"}
        imgs = ocr.decode_keyframes([es_bytes(src.buf, g[0]) for g in gops])
        reads, rois = ocr.read_overlays(imgs)
        cluster, canon = self._cluster_labels([r["label"] for r in reads])
        chan = [cluster.get(r["label"]) for r in reads]
        by_template = self._template_match(imgs, chan, rois.get("label"))
        for k, c in by_template.items():
            chan[k] = c
        single = not canon                      # no camera labels at all
        prnu_info = None
        if single:
            from .. import prnu
            prnu_info = prnu.cluster(imgs)          # try sensor-fingerprint attribution
            if prnu_info["clusters"] >= 2:
                single = False
                for c in range(prnu_info["clusters"]):
                    canon[f"prnu:{c}"] = {"label": f"Sensor cluster {chr(65 + c)}", "channel": 2001 + c}
                chan = [f"prnu:{l}" if l is not None else None for l in prnu_info["labels"]]
            else:
                chan = ["single"] * len(gops)
        times = self._sequence_clocks(gops, chan, [r["clock_s"] for r in reads], fps_hint)
        for k, g in enumerate(gops):
            c = chan[k]
            t0, fps = times.get(k, (None, None))
            for i, f in enumerate(g):
                f.channel = canon[c]["channel"] if c in canon else None
                f.label = canon[c]["label"] if c in canon else None
                if t0 is not None:
                    f.time_ms = int(t0 * 1000 + i * 1000 / fps)
                    f.time_source = "overlay"
        return {"gops": len(gops), "overlay_regions": rois,
                "single_unlabelled_stream": single, "sensor_fingerprint": prnu_info,
                "labels_read_by_ocr": 0 if single else sum(1 for c in chan if c is not None) - len(by_template),
                "labels_by_template_match": len(by_template),
                "clocks_read_by_ocr": sum(1 for r in reads if r["clock_s"] is not None),
                "gops_timed_after_sequencing": len(times),
                "cameras": {str(v["channel"]): v["label"] for v in canon.values()}}

    @staticmethod
    def _cluster_labels(labels, min_ratio=0.75, min_count=3):
        """Group OCR'd labels into cameras: first by the camera number in the label, then by
        text similarity for labels without a readable number. Canonical name = the most
        frequent reading in the group."""
        import difflib
        from collections import Counter
        counts = Counter(l for l in labels if l)
        groups = defaultdict(Counter)          # key -> Counter of readings
        loose = []
        for lab, n in counts.items():
            num = ocr.parse_label(lab)[1]
            if num is not None:
                groups[num][lab] += n
            else:
                loose.append((lab, n))
        sim = lambda a, b: difflib.SequenceMatcher(None, a.upper(), b.upper()).ratio()
        for lab, n in loose:
            best = max(groups, key=lambda k: max(sim(lab, r) for r in groups[k]), default=None)
            if best is not None and max(sim(lab, r) for r in groups[best]) >= min_ratio:
                groups[best][lab] += n
            else:
                groups[f"text:{lab}"][lab] += n
        # A much smaller group is an OCR slip of a bigger one when the camera numbers differ by
        # a confusable digit and the names share a near-identical word.
        def words(g):
            return {w.upper() for r in groups[g] for w in r.split()[1:] if len(w) >= 4}

        def slip(a, b) -> bool:
            if not (isinstance(a, int) and isinstance(b, int)):
                return False
            x, y = f"{a:02d}", f"{b:02d}"
            if len(x) != len(y):
                x, y = x[-len(y):] if len(x) > len(y) else x, y[-len(x):] if len(y) > len(x) else y
                if x == y:
                    return True
            diff = [(p, q) for p, q in zip(x, y) if p != q]
            return len(diff) == 1 and diff[0][1] in CONFUSABLE.get(diff[0][0], "")

        order = sorted(groups, key=lambda k: -sum(groups[k].values()))
        for small in order[::-1]:
            if small not in groups:
                continue
            for big in order:
                if big == small or big not in groups:
                    continue
                if sum(groups[small].values()) <= 0.5 * sum(groups[big].values()) and slip(small, big) and \
                        any(sim(u, v) >= 0.75 for u in words(small) for v in words(big)):
                    groups[big].update(groups.pop(small))
                    break

        def consensus(readings: Counter) -> str:
            return ocr.consensus_label(list(readings.elements()))

        canon, cluster, nxt = {}, {}, 1000
        for key, readings in groups.items():
            if sum(readings.values()) < min_count:
                continue
            name = consensus(readings)
            if not isinstance(key, int) and not re.search(r"[A-Za-z]{3,}", name) or len(set(name.lower())) < 3:
                continue                        # text-only groups must look like a name
            ch = key if isinstance(key, int) else nxt
            nxt += not isinstance(key, int)
            canon[key] = {"label": name, "channel": ch}
            for lab in readings:
                cluster[lab] = key
        return cluster, canon

    @staticmethod
    def _template_match(imgs, chan, box, min_score=0.6, margin=0.1) -> dict:
        """Assign unread labels by comparing overlay glyph pixels with each camera's
        template (glyphs present in most of that camera's readable frames). Score = share of
        the template's glyph pixels present in the query; background clutter cannot lower it."""
        if not box:
            return {}
        import cv2
        import numpy as np
        x0, y0, x1, y1 = box
        glyphs = lambda im: cv2.cvtColor(im[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY) >= 170
        groups = defaultdict(list)
        for im, c in zip(imgs, chan):
            if im is not None and c is not None:
                groups[c].append(glyphs(im))
        tmpl = {c: np.mean(np.stack(v), axis=0) >= 0.7 for c, v in groups.items() if len(v) >= 3}
        tmpl = {c: t for c, t in tmpl.items() if t.sum() >= 30}
        out = {}
        for k, (im, c) in enumerate(zip(imgs, chan)):
            if c is not None or im is None or not tmpl:
                continue
            q = glyphs(im)
            scores = sorted(((float((q & t).sum() / t.sum()), ch) for ch, t in tmpl.items()), reverse=True)
            if scores[0][0] >= min_score and (len(scores) == 1 or scores[0][0] - scores[1][0] >= margin):
                out[k] = scores[0][1]
        return out

    @staticmethod
    def _sequence_clocks(gops, chan, clocks, fps_hint, tol=1.5, reach=30) -> dict:
        """Per camera, in disk order: a clock reading is trusted if it lies within tol
        seconds of the line through its nearest readings on either side; GOPs without a
        trusted reading are interpolated between trusted neighbours (never beyond reach s)."""
        out = {}
        per = defaultdict(list)
        for k in range(len(gops)):
            if chan[k] is not None:
                per[chan[k]].append(k)
        for c, ks in per.items():
            obs = [(i, clocks[k]) for i, k in enumerate(ks) if clocks[k] is not None]
            trusted = []
            for j, (i, t) in enumerate(obs):
                votes = 0
                for a, b in ((j - 2, j - 1), (j - 1, j + 1), (j + 1, j + 2)):
                    if 0 <= a < len(obs) and 0 <= b < len(obs):
                        (ia, ta), (ib, tb) = obs[a], obs[b]
                        if ia != ib:
                            pred = ta + (tb - ta) * (i - ia) / (ib - ia)
                            votes += abs(pred - t) <= tol and 0 <= (tb - ta) / (ib - ia) <= 10
                if votes >= 1:
                    trusted.append((i, t))
            if len(trusted) < 2:
                continue
            steps = [(tb - ta) / (ib - ia) for (ia, ta), (ib, tb) in zip(trusted, trusted[1:]) if tb > ta]
            gop_s = statistics.median(steps) if steps else 1.0
            gop_n = statistics.median(len(gops[k]) for k in ks)
            fps = fps_hint or (gop_n / gop_s if gop_s > 0 else 25.0)
            ti = [i for i, _ in trusted]
            import bisect
            for i, k in enumerate(ks):
                j = bisect.bisect_left(ti, i)
                if j < len(ti) and ti[j] == i:
                    out[k] = (trusted[j][1], fps)
                    continue
                lo = trusted[j - 1] if j > 0 else None
                hi = trusted[j] if j < len(ti) else None
                if lo and hi and hi[1] - lo[1] <= reach and hi[1] >= lo[1]:
                    t = lo[1] + (hi[1] - lo[1]) * (i - lo[0]) / (hi[0] - lo[0])
                elif lo and (i - lo[0]) * gop_s <= reach / 3:
                    t = lo[1] + (i - lo[0]) * gop_s
                elif hi and (hi[0] - i) * gop_s <= reach / 3:
                    t = hi[1] - (hi[0] - i) * gop_s
                else:
                    continue
                out[k] = (t, fps)
        return out

    def recover(self, src, attribute: bool = True) -> Recovery:
        rec = Recovery(self.family, self.name)
        rec.frames = self.carve(src)
        rec.metadata = {"attribution": self.attribute(src, rec.frames) if attribute else "skipped"}
        return rec
