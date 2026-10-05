"""Recovery orchestrator: native parser first, then a universal sweep of every byte the
native parser did not claim, so nothing on the disk is left unexamined."""
from .detect import detect
from .models import Finding, Recovery
from .plugins import NATIVE, UNIVERSAL
from .plugins.generic import gaps
from .source import Source


def recover(src: Source, mode: str = "auto", attribute: bool = True, log=None) -> tuple[dict, Recovery]:
    det = detect(src)
    if log:
        log(f"::stage:: recover {det['family']}")
    plugin = next((p for p in NATIVE if p.name == det["plugin"]), None)
    if mode == "universal" or plugin is None or det["confidence"] < 0.5:
        rec = UNIVERSAL.recover(src, attribute=attribute)
        rec.metadata["mode"] = "universal"
        return det, rec
    rec = plugin.recover(src)
    rec.metadata["mode"] = "native"
    claimed = [(f.offset, f.length) for f in rec.frames]
    unclaimed, regions = 0, []
    for a, b in gaps(src.size, claimed, 4096):
        found = UNIVERSAL.carve(src, a, b)
        if found:
            unclaimed += len(found)
            regions.append({"start": a, "end": b, "units": len(found),
                            "keyframes": sum(f.keyframe for f in found)})
            rec.fragments += [(f.offset, f.length) for f in found]
    rec.metadata["universal_sweep"] = {"unclaimed_video_units": unclaimed, "regions": regions[:200]}
    if unclaimed:
        rec.findings.append(Finding("unclaimed-video", "warning",
            f"{unclaimed} H.264 unit(s) outside any valid {det['family']} frame: remnants of "
            f"overwritten or damaged recordings", {"regions": len(regions)}))
    return det, rec
