"""Case report (HTML + PDF) with timeline, findings and a BSA s.63(4) certificate data sheet."""
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

LEVEL_COLOR = {"index-confirmed": "#1f7a4d", "carved + timestamp": "#2c6cb0", "content-only": "#b07a1f"}


def fmt_ms(ms):
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{ms % 1000:03d}"[:1]


def timeline_svg(tl: dict, clips: list[dict], width: int = 900) -> str:
    timed = [c for c in clips if c["start_ms"] is not None]
    if not timed:
        return "<p>No timestamped video to place on a timeline.</p>"
    t0 = min(c["start_ms"] for c in timed)
    t1 = max(c["end_ms"] for c in timed)
    marks = [i for i in tl["items"] if i["kind"] in ("log", "finding")]
    t0 = min([t0] + [m["start_ms"] for m in marks])
    t1 = max([t1] + [m["start_ms"] for m in marks])
    span = max(1, t1 - t0)
    chans = sorted({c["channel"] for c in timed}, key=lambda c: (c is None, c or 0))
    left, lane, top = 150, 34, 30
    h = top + lane * len(chans) + 60
    X = lambda t: left + (t - t0) / span * (width - left - 20)
    out = [f'<svg viewBox="0 0 {width} {h}" xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    for k, ch in enumerate(chans):
        y = top + k * lane
        name = next((c["label"] for c in timed if c["channel"] == ch and c["label"]), None)
        out.append(f'<text x="8" y="{y + 20}">{escape(name or (f"Camera {ch}" if ch is not None else "Unattributed"))}</text>')
        out.append(f'<line x1="{left}" x2="{width - 20}" y1="{y + 16}" y2="{y + 16}" stroke="#ddd"/>')
        for c in timed:
            if c["channel"] == ch:
                x0, x1 = X(c["start_ms"]), max(X(c["end_ms"]), X(c["start_ms"]) + 2)
                out.append(f'<rect x="{x0:.1f}" y="{y + 6}" width="{x1 - x0:.1f}" height="20" rx="3" '
                           f'fill="{LEVEL_COLOR.get(c["evidence_level"], "#888")}"><title>{c["id"]} '
                           f'{c["evidence_level"]}</title></rect>')
        for e in tl["items"]:
            if e["kind"] == "event" and e.get("channel") == ch:
                out.append(f'<rect x="{X(e["abs_start_ms"]):.1f}" y="{y + 2}" width="{max(3, X(e["abs_end_ms"]) - X(e["abs_start_ms"])):.1f}" '
                           f'height="28" fill="none" stroke="#d62728" stroke-width="2"><title>motion triage</title></rect>')
    yb = top + lane * len(chans)
    for m in marks:
        x = X(m["start_ms"])
        col = "#d62728" if m.get("severity") == "critical" or "format" in m.get("text", "").lower() \
            or "initiali" in m.get("text", "").lower() else "#999"
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{yb}" stroke="{col}" stroke-dasharray="3 3">'
                   f'<title>{escape(m.get("text", ""))}</title></line>')
    for k in range(6):
        t = t0 + span * k / 5
        anchor = "start" if k == 0 else "end" if k == 5 else "middle"
        out.append(f'<text x="{X(t):.1f}" y="{yb + 18}" text-anchor="{anchor}">{fmt_ms(int(t))[11:19]}</text>')
    out.append(f'<text x="{left}" y="{yb + 40}">{fmt_ms(t0)[:10]} · recorder clock</text>')
    lx = width - 420
    for k, (lab, col) in enumerate(LEVEL_COLOR.items()):
        out.append(f'<rect x="{lx + k * 135}" y="{yb + 30}" width="12" height="12" fill="{col}"/>'
                   f'<text x="{lx + k * 135 + 16}" y="{yb + 40}">{lab}</text>')
    out.append("</svg>")
    return "".join(out)


def render(case: dict, out_dir: Path, pdf: bool = True) -> dict:
    env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape())
    env.filters["ms"] = fmt_ms
    html = env.get_template("template.html").render(
        c=case, svg=timeline_svg(case["timeline"], case["clips"]), level_color=LEVEL_COLOR)
    hp = out_dir / "report.html"
    hp.write_text(html, encoding="utf-8")  # Windows: default cp1252 cannot encode "✎" and is not UTF-8
    out = {"html": hp.name}
    if pdf:
        try:
            if sys.platform == "win32" and os.environ.get("WEASYPRINT_DLL_DIRECTORIES"):
                # Windows: other installs (e.g. Tesseract) ship older GTK DLLs of the same name on
                # PATH; put WeasyPrint's Pango directory first so its own libraries load.
                os.environ["PATH"] = os.environ["WEASYPRINT_DLL_DIRECTORIES"] + os.pathsep + os.environ["PATH"]
            from weasyprint import HTML
            HTML(string=html, base_url=str(out_dir)).write_pdf(out_dir / "report.pdf")
            out["pdf"] = "report.pdf"
        except Exception as e:  # weasyprint missing system libraries
            out["pdf_error"] = str(e)
    return out
