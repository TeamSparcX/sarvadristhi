"""Format-family detection: decide what is on the disk from its bytes, never from a label."""
from .plugins import NATIVE, UNIVERSAL
from .source import Source, require_image

# Brands known to ship each on-disk family. Detection reports the family; brand is context.
FAMILY_BRANDS = {
    "hikvision": ["Hikvision", "HiWatch", "Prama Hikvision", "Hikvision OEMs (verify per model)"],
    "dahua-dhav": ["Dahua", "CP Plus (Dahua OEM; verify per model)", "Lorex", "Amcrest",
                   "other Dahua OEMs"],
    "honeywell": ["Honeywell (record layout from one studied NVR; verify per model)"],
    "unknown": ["Uniview, TP-Link VIGI, Matrix, Godrej and others: universal path"],
}


def detect(src: Source) -> dict:
    require_image(src.path)  # Windows: parsers cannot read a physical drive in place
    cands = []
    for p in NATIVE:
        conf, ev = p.detect(src)
        cands.append({"plugin": p.name, "family": p.family, "confidence": round(conf, 3), "evidence": ev})
    conf, ev = UNIVERSAL.detect(src)
    cands.append({"plugin": UNIVERSAL.name, "family": "unknown", "confidence": round(conf, 3), "evidence": ev})
    cands.sort(key=lambda c: -c["confidence"])
    best = cands[0]
    return {"family": best["family"], "plugin": best["plugin"], "confidence": best["confidence"],
            "brands_using_family": FAMILY_BRANDS.get(best["family"], []), "candidates": cands,
            "source_bytes": src.size}
