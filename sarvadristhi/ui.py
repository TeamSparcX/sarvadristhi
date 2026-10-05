"""Local examiner UI: list cases, start an analysis, watch progress, review findings, play
recovered clips, verify custody. Serves on 127.0.0.1 only; no external dependencies."""
import html
import json
import mimetypes
import threading
import traceback
import urllib.parse
import uuid
import queue
import sys
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .custody import Ledger

STATIC = Path(__file__).parent / "ui_static"

CSS = """body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;color:#1b1b1b;background:#f6f6f4}
header{background:#1b1b1b;color:#fff;padding:10px 20px}header a{color:#fff;text-decoration:none;font-weight:600}
main{max-width:1100px;margin:20px auto;padding:0 20px}.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #eee;padding:6px;text-align:left}
input,select{padding:6px;margin:2px 0 8px;width:100%;box-sizing:border-box}button{padding:8px 16px;background:#1f5fa8;color:#fff;border:0;border-radius:6px;cursor:pointer}
.crit{color:#b00020;font-weight:600}.warn{color:#a05a00;font-weight:600}pre{background:#111;color:#ddd;padding:10px;border-radius:6px;max-height:420px;overflow:auto}
video{width:100%;max-width:520px;background:#000}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.muted{color:#666}"""


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body><header><a href='/'>Sarvadristhi {__version__}</a> · DVR/NVR forensics"
            f"</header><main>{body}</main></body></html>").encode()


class App:
    def __init__(self, cases: Path):
        self.cases = Path(cases).resolve()
        self.cases.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.queue = queue.Queue()
        self.lock, self.busy = threading.Lock(), False
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def clean_source(s: str) -> str:
        """Accept paths pasted from Explorer's 'Copy as path' (quoted) or with stray spaces."""
        return s.strip().strip('"').strip("'").strip()

    def case_name(self, case_id: str) -> str:
        return "".join(ch for ch in case_id if ch.isalnum() or ch in "-_")

    def validate(self, form: dict) -> str | None:
        if form.get("kind") in ("live", "kasauti"):
            if not all(str(form.get(k) or "").strip() for k in ("case_id", "examiner")):
                return "Case ID and examiner are required."
            name = self.case_name(form["case_id"])
            if not name or (self.cases / name).exists():
                return f"Case '{name}' already exists or is not a valid ID. Choose a new case ID."
            if form["kind"] == "live":
                lv = form.get("live") or {}
                if not all(str(lv.get(k) or "").strip() for k in ("host", "port", "user", "start", "end")) or not lv.get("channels"):
                    return "Recorder address, port, user, channels and the time window are required."
            else:
                k = form.get("kasauti") or {}
                for key in ("image", "events"):
                    k[key] = self.clean_source(str(k.get(key) or ""))
                    if not k[key] or not Path(k[key]).exists():
                        return f"Not found: {k.get(key) or key}"
            form["source"] = f"{form['kind']}:{(form.get('live') or {}).get('host') or (form.get('kasauti') or {}).get('image')}"
            return None
        if not all(str(form.get(k) or "").strip() for k in ("source", "case_id", "examiner")):
            return "Evidence source, case ID and examiner are all required."
        form["source"] = self.clean_source(form["source"])
        srcs = [self.clean_source(s) for s in (form.get("sources") or [form["source"]]) if str(s).strip()]
        form["sources"] = srcs
        for s in srcs:
            if not Path(s).exists():
                return f"Not found: {s}"
        name = self.case_name(form["case_id"])
        if not name:
            return "Case ID must contain letters or digits."
        if (self.cases / name).exists():
            return f"Case '{name}' already exists. Choose a new case ID (evidence folders are never overwritten)."
        if any(j["case"] == name and j["state"] in ("queued", "running") for j in self.jobs.values()):
            return f"Case '{name}' is already being analysed."
        return None

    def start(self, form: dict) -> str:
        """Queue one analysis. Jobs run one at a time, each in its own process."""
        err = self.validate(form)
        if err:
            raise ValueError(err)
        jid = uuid.uuid4().hex[:8]
        name = self.case_name(form["case_id"])
        with self.lock:             # idle worker: the job starts now, so say so
            state = "queued" if self.busy else "running"
            self.busy = True
        self.jobs[jid] = {"id": jid, "case": name, "log": [], "state": state, "source": form["source"], "kind": form.get("kind"),
                          "queued_at": time.time(), "started": time.time() if state == "running" else None,
                          "finished": None, "proc": None}
        self.queue.put((jid, form))
        return jid

    def cancel(self, jid: str) -> bool:
        j = self.jobs.get(jid)
        if not j or j["state"] not in ("queued", "running"):
            return False
        j["cancel"] = True
        if j["proc"] is not None:
            j["proc"].terminate()
        return True

    NOISE = ("[ WARN:", "rf-detr - ", "UserWarning", "warnings.warn", "FutureWarning", "self.model = ")

    def _worker(self):
        while True:
            jid, form = self.queue.get()
            job = self.jobs[jid]
            if job.get("cancel"):
                job["state"] = "cancelled"
                with self.lock:
                    self.busy = not self.queue.empty()
                continue
            job["state"], job["started"] = "running", time.time()
            spec = {"source": form["source"], "sources": form.get("sources") or [form["source"]],
                    "case_id": form["case_id"], "examiner": form["examiner"],
                    "case_dir": str(self.cases / job["case"]), "mode": form.get("mode", "auto"),
                    "analytics": "analytics" in form, "ai": bool(form.get("ai")), "scenario": bool(form.get("scenario")),
                    "kind": form.get("kind"), "live": form.get("live"), "kasauti": form.get("kasauti")}
            secret = form.pop("password", None)     # never written to the job file or the log
            jobs_dir = self.cases / ".jobs"
            jobs_dir.mkdir(exist_ok=True)
            spec_file = jobs_dir / f"{jid}.json"
            spec_file.write_text(json.dumps(spec), encoding="utf-8")
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            if secret:
                env["SARVADRISTHI_LIVE_PASSWORD"] = secret
            try:
                p = subprocess.Popen([sys.executable, "-m", "sarvadristhi.jobrun", str(spec_file)], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8", errors="replace",
                                     cwd=str(Path(__file__).resolve().parents[1]))
                job["proc"] = p
                for line in p.stdout:
                    line = line.rstrip()
                    if line.startswith("::stage::"):          # structured progress event
                        parts = line.split()
                        ev = {"key": parts[1], "item": next((x for x in parts[2:] if x[:1] == "E" and x[1:].isdigit()), None),
                              "detail": " ".join(x for x in parts[2:] if not (x[:1] == "E" and x[1:].isdigit())),
                              "t": round(time.time() - job["started"], 1)}
                        job.setdefault("stages", []).append(ev)
                        job["log"].append(f"── {ev['key'].upper()}" + (f" · {ev['item']}" if ev["item"] else "")
                                          + (f" · {ev['detail']}" if ev["detail"] else "") + f" · t+{ev['t']}s ──")
                        continue
                    if line and not any(n in line for n in self.NOISE):
                        job["log"].append(line)
                p.wait()
                job["state"] = "cancelled" if job.get("cancel") else "done" if p.returncode == 0 else "failed"
            except Exception as e:
                job["log"].append("ERROR: " + str(e))
                job["log"].append(traceback.format_exc()[-1500:])
                job["state"] = "failed"
            job["finished"], job["proc"] = time.time(), None
            with self.lock:
                self.busy = not self.queue.empty()

    def public(self, jid: str) -> dict | None:
        j = self.jobs.get(jid)
        if not j:
            return None
        out = {k: v for k, v in j.items() if k != "proc"}
        if j["state"] == "queued":
            ahead = [x for x in self.jobs.values() if x["state"] in ("queued", "running") and x["queued_at"] < j["queued_at"]]
            out["position"] = len(ahead)
        out["elapsed_s"] = round((j["finished"] or time.time()) - (j["started"] or time.time()), 1)
        return out

    def case_dirs(self):
        return sorted((p for p in self.cases.iterdir() if (p / "case.json").exists()), key=lambda p: -p.stat().st_mtime)

    def safe(self, case: str, rel: str = "") -> Path | None:
        p = (self.cases / case / rel).resolve()
        # Windows: Path containment must not depend on a hard-coded slash separator.
        return p if p != self.cases and p.is_relative_to(self.cases) and p.exists() else None


def handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
            if not parts:
                return self.app_page()
            if parts[0] == "classic":
                return self.home()
            if parts[0] == "api":
                return self.api(parts[1:], urllib.parse.parse_qs(u.query))
            if parts[0] == "static":
                f = (STATIC / "/".join(parts[1:])).resolve()
                if f.is_file() and f.is_relative_to(STATIC.resolve()):
                    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                    return self.send(200, f.read_bytes(), ctype, {"Cache-Control": "max-age=3600"})
                return self.send(404, b"")
            if parts[0] == "job" and len(parts) == 2:
                return self.job(parts[1])
            if parts[0] == "case" and len(parts) == 2:
                return self.case(parts[1])
            if parts[0] == "case" and len(parts) >= 4 and parts[2] == "file":
                return self.file(parts[1], "/".join(parts[3:]))
            if parts[0] == "case" and len(parts) == 3 and parts[2] == "verify":
                return self.verify(parts[1])
            self.send(404, page("Not found", "<p>Not found.</p>"))

        def do_POST(self):
            if self.path == "/api/analyze":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    form = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self.json({"error": "bad request"}, 400)
                if form.get("ai"):
                    form["ai"] = "on"
                else:
                    form.pop("ai", None)
                form["analytics"] = "on"
                try:
                    return self.json({"job": app.start(form)})
                except ValueError as e:
                    return self.json({"error": str(e)}, 400)
            parts = [urllib.parse.unquote(p) for p in self.path.strip("/").split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "case"] and parts[3] in ("review", "report"):
                cj = app.safe(parts[2], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if parts[3] == "review":
                    return self.json(review(cj.parent, body))
                return self.json(regenerate_report(cj.parent))
            if len(parts) == 4 and parts[:2] == ["api", "job"] and parts[3] == "cancel":
                return self.json({"cancelled": app.cancel(parts[2])})
            if parts == ["api", "live", "mock"]:
                return self.json(start_mock(app))
            if parts == ["api", "kasauti", "rehearsal"]:
                return self.json(kasauti_rehearsal(app))
            if len(parts) == 4 and parts[:2] == ["api", "case"] and parts[3] in ("notes", "offset", "segment"):
                cj = app.safe(parts[2], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    b = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self.json({"error": "bad request"}, 400)
                from . import examiner
                try:
                    if parts[3] == "notes":
                        return self.json(examiner.add_note(cj.parent, b.get("clip", ""), b.get("from_s", 0), b.get("to_s", 0),
                                                           b.get("text", ""), b.get("examiner", "")))
                    if parts[3] == "offset":
                        return self.json(examiner.set_offset(cj.parent, b.get("evidence", "E1"), b.get("offset_s", 0),
                                                             b.get("reason", ""), b.get("examiner", ""), b.get("reference", "")))
                    return self.json(examiner.export_segment(cj.parent, b.get("clip", ""), b.get("from_s", 0), b.get("to_s", 0),
                                                             b.get("purpose", ""), b.get("examiner", "")))
                except (ValueError, RuntimeError) as e:
                    return self.json({"error": str(e)}, 400)
            if len(parts) == 4 and parts[:2] == ["api", "case"] and parts[3] in ("redact", "potli"):
                cj = app.safe(parts[2], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    b = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self.json({"error": "bad request"}, 400)
                from . import anveshan
                try:
                    if parts[3] == "redact":
                        return self.json(anveshan.redact(cj.parent, str(b.get("clip", "")), [str(x) for x in b.get("keep") or []],
                                                         b.get("examiner", ""), b.get("purpose", "")))
                    return self.json(anveshan.package(cj.parent, b.get("examiner", "")))
                except (ValueError, RuntimeError) as e:
                    return self.json({"error": str(e)}, 400)
            if len(parts) == 4 and parts[:2] == ["api", "case"] and parts[3] in ("ask", "capsule", "replay"):
                cj = app.safe(parts[2], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self.json({"error": "bad request"}, 400)
                from . import capsule, workbench
                try:
                    if parts[3] == "ask":
                        return self.json(workbench.ask(cj.parent, str(body.get("q", ""))[:400]))
                    if parts[3] == "capsule":
                        return self.json(capsule.build(cj.parent, log=lambda m: None))
                    cap = cj.parent / "capsule"
                    if not (cap / "capsule.json").exists():
                        return self.json({"error": "build the capsule first"}, 400)
                    r = capsule.replay(cap, tamper=body.get("tamper") or None, log=lambda m: None)
                    Ledger(cj.parent / "custody.jsonl").append("replay-verified", reproduced=r["reproduced"], total=r["total"],
                                                               signature_ok=r["signature_ok"], simulated_tamper=body.get("tamper") or None)
                    return self.json(r)
                except Exception as e:      # report, never crash the server
                    return self.json({"error": f"{type(e).__name__}: {e}"}, 500)
            if self.path != "/analyze":
                return self.send(404, b"")
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
            try:
                jid = app.start(form)
            except ValueError as e:
                return self.send(400, page("Cannot start", f"<p>{html.escape(str(e))}</p>"))
            self.send(303, b"", extra={"Location": f"/job/{jid}"})

        def home(self):
            rows = ""
            for p in app.case_dirs():
                c = json.loads((p / "case.json").read_text())
                crit = sum(f["severity"] == "critical" for f in c["findings"])
                rows += (f"<tr><td><a href='/case/{urllib.parse.quote(p.name)}'>{html.escape(c['case']['case_id'])}</a></td>"
                         f"<td>{html.escape(c['detection']['family'])}</td><td>{c['recovery']['frames']}</td>"
                         f"<td>{len(c['clips'])}</td><td class='{'crit' if crit else ''}'>{crit}</td>"
                         f"<td>{c['generated_utc']}</td></tr>")
            jobs = "".join(f"<li><a href='/job/{j['id']}'>{html.escape(j['case'])}</a> — {j['state']}</li>"
                           for j in app.jobs.values())
            body = f"""<div class='grid'><div class='card'><h3>New analysis</h3>
<form method='post' action='/analyze'>
<label>Evidence source (image, .E01, export file or block device)</label><input name='source' required>
<label>Case ID</label><input name='case_id' required><label>Examiner</label><input name='examiner' required>
<label>Mode</label><select name='mode'><option value='auto'>auto (native parser + universal sweep)</option>
<option value='universal'>universal only (unknown recorder)</option></select>
<label><input type='checkbox' name='analytics' checked style='width:auto'> motion triage</label>
<label><input type='checkbox' name='ai' style='width:auto'> AI triage (optional models)</label>
<button>Analyse</button></form></div>
<div class='card'><h3>Running and recent jobs</h3><ul>{jobs or '<li class=muted>none</li>'}</ul>
<p class='muted'>Evidence is opened read-only and hashed before and after analysis. Use a hardware write-blocker for original drives.</p></div></div>
<div class='card'><h3>Cases</h3><table><tr><th>Case</th><th>Format</th><th>Frames</th><th>Clips</th><th>Critical findings</th><th>Generated (UTC)</th></tr>{rows}</table></div>"""
            self.send(200, page("sarvadristhi", body))

        def job(self, jid):
            j = app.jobs.get(jid)
            if not j:
                return self.send(404, page("No job", "<p>Unknown job.</p>"))
            link = f"<p><a href='/case/{urllib.parse.quote(j['case'])}'>Open case</a></p>" if j["state"] == "done" else ""
            refresh = "<meta http-equiv='refresh' content='2'>" if j["state"] == "running" else ""
            self.send(200, page("Job", f"{refresh}<div class='card'><h3>{html.escape(j['case'])} — {j['state']}</h3>{link}"
                                       f"<pre>{html.escape(chr(10).join(j['log']))}</pre></div>"))

        def case(self, name):
            p = app.safe(name, "case.json")
            if not p:
                return self.send(404, page("No case", "<p>Unknown case.</p>"))
            c = json.loads(p.read_text())
            from .report import timeline_svg
            q = urllib.parse.quote(name)
            fnd = "".join(f"<tr><td class='{ {'critical': 'crit', 'warning': 'warn'}.get(f['severity'], '') }'>{f['severity']}</td>"
                          f"<td>{html.escape(f['kind'])}</td><td>{html.escape(f['summary'])}</td></tr>" for f in c["findings"])
            vids = ""
            for k in c["clips"]:
                src = k.get("mp4_file")
                media = (f"<video controls preload='metadata' src='/case/{q}/file/clips/{urllib.parse.quote(src)}'></video>"
                         if src else f"<p class='muted'>{html.escape(k.get('mp4_error', 'not exported'))}</p>")
                vids += (f"<div class='card'><b>{k['id']}</b> · camera {k['channel'] if k['channel'] is not None else '?'}"
                         f" · {k['frames']} frames · {k['evidence_level']}<br>{media}<div class='muted' style='font-size:12px'>"
                         f"SHA-256 {k.get('mp4_sha256') or k.get('raw_sha256', '')}</div></div>")
            body = (f"<h2>{html.escape(c['case']['case_id'])}</h2><p>{html.escape(c['detection']['family'])} · "
                    f"{c['recovery']['frames']} frames · examiner {html.escape(c['case']['examiner'])} · "
                    f"<a href='/case/{q}/file/report.pdf'>PDF report</a> · <a href='/case/{q}/file/report.html'>HTML report</a> · "
                    f"<a href='/case/{q}/verify'>verify custody and clip hashes</a></p>"
                    f"<div class='card'><h3>Findings</h3><table>{fnd or '<tr><td>none</td></tr>'}</table></div>"
                    f"<div class='card'><h3>Timeline</h3>{timeline_svg(c['timeline'], c['clips'])}</div>"
                    f"<h3>Recovered clips</h3><div class='grid'>{vids}</div>")
            self.send(200, page(c["case"]["case_id"], body))

        def verify(self, name):
            d = app.safe(name)
            if not d:
                return self.send(404, page("No case", ""))
            ok, msg = Ledger(d / "custody.jsonl").verify()
            from .acquire import hash_file
            c = json.loads((d / "case.json").read_text())
            rows = ""
            for k in c["clips"]:
                if k.get("mp4_file"):
                    now = hash_file(d / "clips" / k["mp4_file"])["sha256"]
                    rows += f"<tr><td>{k['mp4_file']}</td><td>{'OK' if now == k['mp4_sha256'] else '<b class=crit>CHANGED</b>'}</td></tr>"
            self.send(200, page("Verify", f"<div class='card'><h3>Custody ledger: {'<span>intact</span>' if ok else '<span class=crit>TAMPERED</span>'}</h3>"
                                          f"<p>{html.escape(msg)}</p><table><tr><th>Clip</th><th>Hash</th></tr>{rows}</table></div>"))

        def json(self, obj, code=200):
            self.send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

        def app_page(self):
            body = (STATIC / "app.html").read_text(encoding="utf-8").replace(
                "/*BOOT*/null", json.dumps({"cases": case_list(app), "version": __version__}))
            self.send(200, body.encode(), extra={"Cache-Control": "no-store"})

        def api(self, parts, q=None):
            q = q or {}
            if len(parts) in (3, 4) and parts[0] == "case" and parts[2] == "anveshan":
                cj = app.safe(parts[1], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                from . import anveshan
                try:
                    if len(parts) == 4:
                        return self.json(anveshan.search(cj.parent, parts[3]))
                    return self.json({"candidates": anveshan.candidates(cj.parent),
                                      "redactions": anveshan.redactions(cj.parent),
                                      "packages": anveshan.packages(cj.parent), "disclaimer": anveshan.DISCLAIMER})
                except ValueError as e:
                    return self.json({"error": str(e)}, 400)
            if len(parts) == 4 and parts[0] == "case" and parts[2] == "diskmap":
                return self.json(disk_map(app, parts[1], parts[3], int((q.get("bins") or ["1600"])[0])))
            if len(parts) == 4 and parts[0] == "case" and parts[2] == "cell":
                return self.json(cell_info(app, parts[1], parts[3], int((q.get("a") or ["0"])[0]), int((q.get("b") or ["0"])[0])))
            if len(parts) == 4 and parts[0] == "case" and parts[2] == "hex":
                return self.json(hex_view(app, parts[1], parts[3], int((q.get("offset") or ["0"])[0])))
            if parts == ["cases"]:
                return self.app_cases()
            if parts == ["capabilities"]:
                from .kasauti import promote
                from .workbench import capabilities
                return self.json(promote(capabilities(), app.cases / ".kasauti"))
            if parts == ["standards"]:
                return self.json(json.loads((Path(__file__).parent / "standards.json").read_text(encoding="utf-8")))
            if parts == ["kasauti"]:
                from .kasauti import runs
                return self.json(runs(app.cases / ".kasauti"))
            if parts == ["kasauti", "template.csv"]:
                from .kasauti import TEMPLATE
                return self.send(200, TEMPLATE.encode(), "text/csv; charset=utf-8",
                                 {"Content-Disposition": "attachment; filename=kasautipeeth_event_log.csv"})
            if len(parts) == 3 and parts[0] == "case" and parts[2] in ("notes", "offsets", "segments"):
                cj = app.safe(parts[1], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                from . import examiner
                return self.json({"notes": examiner.notes, "offsets": examiner.offsets, "segments": examiner.segments}[parts[2]](cj.parent))
            if len(parts) == 4 and parts[0] == "case" and parts[2] in ("xray", "frame"):
                cj = app.safe(parts[1], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                from . import kanika
                try:
                    if parts[2] == "xray":
                        return self.json(kanika.frames(cj.parent, parts[3]))
                    return self.json(kanika.header(cj.parent, parts[3], int((q.get("offset") or ["0"])[0])))
                except (StopIteration, ValueError) as e:
                    return self.json({"error": str(e) or "not found"}, 404)
            if len(parts) == 3 and parts[0] == "case" and parts[2] in ("hypotheses", "reconcile", "capsule"):
                cj = app.safe(parts[1], "case.json")
                if not cj:
                    return self.json({"error": "no such case"}, 404)
                from . import workbench
                if parts[2] == "hypotheses":
                    return self.json(workbench.hypotheses(cj.parent))
                if parts[2] == "reconcile":
                    return self.json(workbench.reconcile(cj.parent))
                cap = cj.parent / "capsule" / "capsule.json"
                if not cap.exists():
                    return self.json({"exists": False})
                from .capsule import load
                body, ok = load(cap.parent)
                return self.json({"exists": True, "signature_ok": ok, "created_utc": body["created_utc"],
                                  "key_fingerprint": body["key_fingerprint"], "clips": len(body["clips"]),
                                  "ranges": sum(len(k["ranges"]) for k in body["clips"]), "items": body["items"],
                                  "definition": body["definition"], "problems": body["problems"],
                                  "clip_ids": [k["id"] for k in body["clips"]]})
            if len(parts) == 4 and parts[0] == "case" and parts[2] == "still":
                from .workbench import still
                if not app.safe(parts[1], "case.json"):
                    return self.send(404, b"")
                try:
                    at = float((q.get("t") or ["1"])[0])
                except ValueError:
                    at = 1.0
                f = still(app.cases, parts[1], parts[3], max(0.0, min(at, 3600.0)))
                if not f:
                    return self.send(404, b"")
                return self.send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "max-age=86400"})
            if len(parts) == 2 and parts[0] == "case":
                p = app.safe(parts[1], "case.json")
                if not p:
                    return self.json({"error": "no such case"}, 404)
                c = json.loads(p.read_text(encoding="utf-8"))
                if not c.get("ai"):
                    a = app.safe(parts[1], "ai/ai.json")
                    c["ai"] = json.loads(a.read_text(encoding="utf-8")) if a else {}
                led = Ledger(p.parent / "custody.jsonl")
                ok, status = led.verify()
                c["custody_now"] = {"ok": ok, "status": status, "entries": led.entries()}
                r = p.parent / "reviews.json"
                c["reviews"] = json.loads(r.read_text(encoding="utf-8")) if r.exists() else {}
                return self.json(c)
            if len(parts) == 2 and parts[0] == "job":
                j = app.public(parts[1])
                return self.json(j or {"error": "no such job"}, 200 if j else 404)
            if parts == ["jobs"]:
                return self.json([app.public(k) for k in app.jobs])
            if parts == ["sources"]:
                return self.json(sources(app, drives=False))
            if parts == ["drives"]:
                return self.json(sources(app, drives=True)["drives"])
            if parts == ["caselist"]:
                return self.json(case_list(app))
            return self.json({"error": "unknown endpoint"}, 404)

        def app_cases(self):
            return self.json([p.name for p in app.case_dirs()])

        def file(self, name, rel):
            p = app.safe(name, rel)
            if not p or not p.is_file():
                return self.send(404, b"")
            size = p.stat().st_size
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                a = int(a or 0)
                b = min(int(b) if b else a + 4 * 1024 * 1024 - 1, size - 1)   # open-ended: serve 4 MB pieces
                with p.open("rb") as f:
                    f.seek(a)
                    data = f.read(b - a + 1)
                return self.send(206, data, ctype, {"Content-Range": f"bytes {a}-{b}/{size}", "Accept-Ranges": "bytes"})
            self.send(200, p.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
    return H


def review(case_dir: Path, body: dict) -> dict:
    """Examiner decision on an AI lead: accepted | rejected | unresolved, with a note; custody-logged."""
    key, decision = str(body.get("key", ""))[:64], body.get("decision")
    if not key or decision not in ("accepted", "rejected", "unresolved"):
        return {"error": "key and decision (accepted/rejected/unresolved) required"}
    rp = case_dir / "reviews.json"
    reviews = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    entry = {"decision": decision, "note": str(body.get("note", ""))[:500], "examiner": str(body.get("examiner", ""))[:80],
             "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "statement": str(body.get("statement", ""))[:400]}
    reviews[key] = entry
    rp.write_text(json.dumps(reviews, indent=1), encoding="utf-8")
    Ledger(case_dir / "custody.jsonl").append("examiner-review", key=key, **entry)
    return {"ok": True, "reviews": reviews}


_MOCK = {}


def start_mock(app) -> dict:
    """A simulated recorder on 127.0.0.1 serving Pramanshala exports through the real Dahua download
    API and ONVIF, for demonstrating Live acquisition without hardware. Labelled as simulated everywhere."""
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    exports = root / "kit" / "exports"
    if not (exports / "dahua").exists():
        return {"error": "kit/exports/dahua not found: build or download the demo data first"}
    if "nvr" not in _MOCK:
        spec = importlib.util.spec_from_file_location("mock_nvr", root / "tests" / "mock_nvr.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        nvr = mod.MockNVR(exports, manufacturer="Dahua")
        nvr.__enter__()
        _MOCK["nvr"] = nvr
    nvr = _MOCK["nvr"]
    start, end = "2026-09-28 21:14:20", "2026-09-28 21:15:00"     # inside the Pramanshala recording window
    return {"host": "127.0.0.1", "port": nvr.port, "user": nvr.user, "password": nvr.password, "vendor": "dahua",
            "channels": [1, 2, 3], "start": start, "end": end, "simulated": True,
            "note": "Simulated recorder (tests/mock_nvr.py) serving modelled Dahua exports; demo credentials only."}


def regenerate_report(case_dir: Path) -> dict:
    """Re-render the report from case data plus examiner reviews, examiner findings,
    clock corrections, exported segments, key frames and the replay
    capsule; custody-logged with the report's own hashes."""
    from . import examiner, report
    from .acquire import hash_file
    c = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    rp = case_dir / "reviews.json"
    c["reviews"] = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
    c["report_frames"] = examiner.report_frames(case_dir, c)
    c["sakshilekh"] = examiner.notes(case_dir)
    c["kaalshodhan"] = examiner.offsets(case_dir)["history"]
    c["khandmudra"] = examiner.segments(case_dir)
    cap = case_dir / "capsule" / "capsule.json"
    if cap.exists():
        from .capsule import load
        body, ok = load(cap.parent)
        c["punarnirmiti"] = {"signature_ok": ok, "key_fingerprint": body["key_fingerprint"], "created_utc": body["created_utc"],
                             "clips": len(body["clips"]), "ranges": sum(len(k["ranges"]) for k in body["clips"])}
    led = Ledger(case_dir / "custody.jsonl")
    ok, status = led.verify()
    c["custody"] = {"ok": ok, "status": status, "entries": led.entries()}
    rep = report.render(c, case_dir, pdf=True)
    hashes = {k: hash_file(case_dir / v)["sha256"] for k, v in rep.items() if k in ("html", "pdf")}
    led.append("report-regenerated", accepted_leads=sum(v["decision"] == "accepted" for v in c["reviews"].values()),
               **{f"{k}_sha256": v for k, v in hashes.items()})
    return {"ok": True, "report": rep, "hashes": hashes}


def kasauti_rehearsal(app) -> dict:
    """Write an event log from Pramanshala ground truth for a modelled disk, as an examiner would,
    so the kit can be rehearsed end to end. Clearly a rehearsal: it is not marked physical."""
    from .kasauti import events_from_ground_truth
    root = Path(__file__).resolve().parents[1]
    for img, gt in ((root / "kit" / "h265" / "images" / "hikvision_h265_formatted.img", root / "kit" / "h265" / "ground_truth" / "hikvision_h265_formatted.csv"),
                    (root / "kit" / "images" / "hikvision_formatted.img", root / "kit" / "ground_truth" / "hikvision_formatted.csv")):
        if img.exists() and gt.exists():
            import csv as _csv
            with open(gt, newline="", encoding="utf-8") as f:
                last = max(int(r["true_time_ms_utc"]) for r in _csv.DictReader(f) if r["origin"] == "original")
            out = app.cases / ".kasauti_rehearsal" / f"{img.stem}_event_log.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            events_from_ground_truth(gt, out, format_time_ms=last + 600_000)
            return {"image": str(img), "events": str(out), "model": img.stem}
    return {"error": "no modelled Hikvision disk with ground truth found under kit/"}


def disk_map(app, case: str, eid: str, bins: int = 1600) -> dict:
    """Bin an evidence image by byte offset: what the tool found in each region, from frames.csv
    and the parsed structures. Read-only; nothing here touches the image itself."""
    import csv
    cj = app.safe(case, "case.json")
    if not cj:
        return {"error": "no such case"}
    c = json.loads(cj.read_text(encoding="utf-8"))
    ev = next((e for e in c.get("evidence_items", []) if e["id"] == eid), None)
    if ev is None:
        return {"error": "no such evidence item"}
    size = ev["bytes"] or 1
    bins = max(64, min(bins, 4096))
    step = size / bins
    cats = ["index-confirmed", "carved + timestamp", "content-only", "metadata", "deleted file", "live file"]
    grid = [[0] * len(cats) for _ in range(bins)]

    def mark(a, n, cat):
        k = cats.index(cat)
        b0, b1 = int(a / step), int(min(size - 1, a + max(1, n) - 1) / step)
        for b in range(max(0, b0), min(bins - 1, b1) + 1):
            grid[b][k] += max(1, n)
    fc = app.safe(case, f"evidence/{eid}/frames.csv")
    regions = []
    if fc:
        with open(fc, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                lvl = r["evidence_level"] if r["evidence_level"] in cats else "content-only"
                mark(int(r["offset"]), int(r["length"]), lvl)
    ms = (ev.get("recovery") or {}).get("master_sector") or {}
    for name, off, ln in (("master sector", ms.get("offset"), 512), ("system log", ms.get("logs_offset"), ms.get("logs_size")),
                          ("HIKBTREE", ms.get("btree1_offset"), ms.get("btree1_size")),
                          ("HIKBTREE backup", ms.get("btree2_offset"), ms.get("btree2_size"))):
        if off is not None and ln:
            mark(off, ln, "metadata")
            regions.append({"name": name, "offset": off, "length": ln})
    fs = ev.get("filesystem")
    if fs:
        cs = fs["cluster_size"]
        for d in fs["deleted"]:
            for a, n in (d.get("clusters") or {}).get("runs", []):
                mark(fs["offset"] + a * cs, n * cs, "deleted file")
                regions.append({"name": f"deleted: {d['name']}", "offset": fs["offset"] + a * cs, "length": n * cs})
        for a, n in fs.get("live_runs", []):
            mark(fs["offset"] + a * cs, n * cs, "live file")
    cells = []
    for b, row in enumerate(grid):
        tot = sum(row)
        # k: bitmask of every category present in the slice (bit i = cats[i]), for the index-vs-disk view
        cells.append({"b": b, "cat": cats[max(range(len(cats)), key=lambda k: row[k])] if tot else "empty",
                      "fill": round(min(1.0, tot / step), 3), "k": sum(1 << i for i, v in enumerate(row) if v)})
    return {"evidence": eid, "name": ev["name"], "bytes": size, "bins": bins, "bytes_per_bin": step,
            "categories": cats + ["empty"], "cells": cells, "regions": regions[:60], "family": ev["family"]}


def cell_info(app, case: str, eid: str, a: int, b: int) -> dict:
    """What lives in bytes [a, b) of an evidence image: recovered frames grouped by clip, known
    structures and filesystem files. Read from the case's own records; the image is not opened."""
    import csv
    cj = app.safe(case, "case.json")
    if not cj:
        return {"error": "no such case"}
    c = json.loads(cj.read_text(encoding="utf-8"))
    items = c.get("evidence_items") or [{"id": "E1", "dir": "", "recovery": c["recovery"], "filesystem": c.get("filesystem"),
                                         "name": c["case"]["case_id"]}]
    ev = next((e for e in items if e["id"] == eid), None)
    if ev is None:
        return {"error": "no such evidence item"}
    clips = [k for k in c["clips"] if k.get("evidence", "E1") == eid]
    groups = {}
    fc = cj.parent / (ev.get("dir") or "") / "frames.csv"
    if fc.exists():
        with open(fc, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                o, n = int(r["offset"]), int(r["length"])
                if o + n <= a or o >= b:
                    continue
                ch = int(r["channel"]) if r["channel"] else None
                t = int(r["time_ms"]) if r["time_ms"] else None
                k = next((x for x in clips if t is not None and x.get("start_ms") is not None and
                          x.get("local_channel", x.get("channel")) == ch and x["start_ms"] <= t <= x["end_ms"]), None)
                key = k["id"] if k else f"unassigned CH{ch}"
                g = groups.setdefault(key, {"clip": k["id"] if k else None, "label": (k or {}).get("label") or f"channel {ch}",
                                            "frames": 0, "keyframes": 0, "levels": {}, "first_offset": o, "first_ms": t,
                                            "last_ms": t, "playable": bool(k and k.get("mp4_file"))})
                g["frames"] += 1
                g["keyframes"] += int(r["keyframe"])
                g["levels"][r["evidence_level"]] = g["levels"].get(r["evidence_level"], 0) + 1
                g["first_offset"] = min(g["first_offset"], o)
                if t is not None:
                    g["first_ms"] = t if g["first_ms"] is None else min(g["first_ms"], t)
                    g["last_ms"] = t if g["last_ms"] is None else max(g["last_ms"], t)
    rec = ev.get("recovery") or {}
    ms = rec.get("master_sector") or {}
    structs = []
    for name, off, ln in (("master sector", ms.get("offset"), 512), ("system log", ms.get("logs_offset"), ms.get("logs_size")),
                          ("HIKBTREE index", ms.get("btree1_offset"), ms.get("btree1_size")),
                          ("HIKBTREE backup", ms.get("btree2_offset"), ms.get("btree2_size"))):
        if off is not None and ln and off < b and off + ln > a:
            structs.append({"name": name, "offset": off, "length": ln})
    files = []
    fs = ev.get("filesystem")
    if fs:
        cs = fs["cluster_size"]
        for d in fs.get("deleted", []):
            for x, n in (d.get("clusters") or {}).get("runs", []):
                s = fs["offset"] + x * cs
                if s < b and s + n * cs > a:
                    files.append({"path": d["path"], "deleted": True, "offset": s, "verdict": d.get("verdict")})
    return {"evidence": eid, "from": a, "to": b, "clips": sorted(groups.values(), key=lambda g: -g["frames"]),
            "structures": structs, "files": files}


def hex_view(app, case: str, eid: str, offset: int, length: int = 256) -> dict:
    """256 bytes of the ORIGINAL evidence image at an offset, opened read-only."""
    cj = app.safe(case, "case.json")
    if not cj:
        return {"error": "no such case"}
    c = json.loads(cj.read_text(encoding="utf-8"))
    ev = next((e for e in c.get("evidence_items", []) if e["id"] == eid), None)
    if ev is None:
        return {"error": "no such evidence item"}
    p = Path(ev["path"])
    root = Path(__file__).resolve().parents[1]
    if not p.is_absolute():
        p = root / p
    if not p.exists() and "kit" in p.parts:      # case built on another machine: use this project's kit\
        p = root.joinpath(*p.parts[p.parts.index("kit"):])
    if not p.exists():
        return {"error": f"source image not found on this machine: {ev['path']}"}
    offset = max(0, min(offset, (ev["bytes"] or 1) - 1)) // 16 * 16
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    rows = [{"offset": offset + i, "hex": data[i:i + 16].hex(" "),
             "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i + 16])}
            for i in range(0, len(data), 16)]
    return {"evidence": eid, "offset": offset, "rows": rows, "source": ev["path"], "read_only": True}


def case_list(app) -> list[dict]:
    cases = []
    for p in app.case_dirs():
        try:
            c = json.loads((p / "case.json").read_text(encoding="utf-8"))
            vids = [k for k in c["clips"] if k.get("mp4_file")]
            cover = max(vids, key=lambda k: k.get("frames") or 0) if vids else None
            cases.append({"dir": p.name, "case_id": c["case"]["case_id"], "kind": c["case"].get("kind", "recorder-disk"),
                          "cover": cover["mp4_file"] if cover else None, "cover_label": (cover or {}).get("label"),
                          "examiner": c["case"].get("examiner"), "items": len(c.get("evidence_items") or [1]),
                          "poisons": [sum(1 for x in c.get("poison_scorecard") or [] if x.get("caught")),
                                      len(c.get("poison_scorecard") or [])],
                          "family": c["detection"]["family"], "frames": c["recovery"]["frames"],
                          "clips": len(c["clips"]), "critical": sum(f["severity"] == "critical" for f in c["findings"]),
                          "generated": c["generated_utc"], "ai": bool(c.get("ai"))})
        except (ValueError, KeyError):
            continue
    return cases


_DRIVES = {"t": 0, "v": []}


def sources(app, drives: bool = True) -> dict:
    """Evidence presets (pramanshala, demo media) and attached drives the examiner can pick."""
    root = Path(__file__).resolve().parents[1]
    kit = root / "kit"
    presets = []
    sc = kit / "scenario"
    items = [sc / "images" / n for n in ("DVR-A_hikvision.img", "DVR-B_dahua.img", "USB_shiftB.vhd")]
    if (sc / "scenario.json").exists() and all(p.exists() for p in items):
        presets.append({"title": "OPERATION DEMO · one run, everything the PS asks",
                        "sub": "Real CCTV footage (MEVA, CC-BY-4.0) on a deliberately poisoned Hikvision DVR, Dahua DVR and "
                               "USB stick: format, overwrite, clock change, deleted export, edited export, disguised file",
                        "paths": [str(p) for p in items], "path": str(items[0]), "scenario": True,
                        "bytes": sum(p.stat().st_size for p in items)})
    nice = {"usb_demo.vhd": ("USB stick (NTFS) - one clip deleted", "Deleted-file recovery with exact deletion time"),
            "realcams": ("Real CCTV - 2 cameras", "Real people; full AI layers L1-L4"),
            "hikvision_overwritten.img": ("Hikvision DVR - formatted + overwritten", "Native parser, deleted footage"),
            "dahua_overwritten.img": ("Dahua / CP Plus DVR - overwritten", "DHAV frames, torn frame at boundary"),
            "hikvision_intact.img": ("Hikvision DVR - intact", "Baseline"),
            "dahua_no_overlay.img": ("Dahua - no overlay, 3 cameras", "Sensor-noise fingerprint attribution")}
    # real MEVA footage in modelled disks (pramanshala.realdemos); the synthetic versions are the fallback
    extra = [(("realdemo/images/cpplus_label_on_real_hikvision.img", "images/cpplus_label_on_hikvision.img"),
              "Relabelled DVR - 'CP PLUS' string, Hikvision structures", "Format hypotheses: the bytes decide, the label is reported as a conflict"),
             (("realdemo/images/hikvision_h265_formatted.img", "h265/images/hikvision_h265_formatted.img"),
              "Hikvision DVR - H.265, formatted", "Real CCTV in H.265; recovery with recorder timestamps"),
             (("realdemo/images/dahua_h265_overwritten.img", "h265/images/dahua_h265_overwritten.img"),
              "Dahua / CP Plus DVR - H.265, overwritten", "Real CCTV in H.265 DHAV frames; torn frame at the boundary"),
             (("realdemo/images/honeywell_overwritten.img", "honeywell/images/honeywell_overwritten.img"),
              "Honeywell NVR - overwritten", "Real CCTV in Honeywell records (DFRWS USA 2026); camera from overlay OCR")]
    for rels, title, sub in extra:
        p = next((kit / r for r in rels if (kit / r).exists()), None)
        if p:
            real = "realdemo" in p.parts
            presets.append({"title": title + ("" if real else " (synthetic picture)"), "sub": sub, "path": str(p),
                            "bytes": p.stat().st_size})
    for name, (title, sub) in nice.items():
        for p in (kit / name, kit / "images" / name):
            if p.exists():
                size = sum(f.stat().st_size for f in p.iterdir()) if p.is_dir() else p.stat().st_size
                presets.append({"title": title, "sub": sub, "path": str(p), "bytes": size})
    if not drives:
        return {"presets": presets, "drives": []}
    if time.time() - _DRIVES["t"] < 30:
        return {"presets": presets, "drives": _DRIVES["v"]}
    drives = []
    if os.name == "nt":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_DiskDrive | Select-Object DeviceID,Model,Size,InterfaceType,MediaType "
                                "| ConvertTo-Json"], capture_output=True, text=True, timeout=15)
            d = json.loads(r.stdout or "[]")
            for x in d if isinstance(d, list) else [d]:
                drives.append({"path": x["DeviceID"], "model": x.get("Model"), "bytes": x.get("Size"),
                               "bus": x.get("InterfaceType"), "media": x.get("MediaType")})
        except Exception:
            pass
    _DRIVES.update(t=time.time(), v=drives)
    return {"presets": presets, "drives": drives}


def serve(cases: Path, port: int = 8765, open_browser: bool = True):
    app = App(cases)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler(app))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Sarvadristhi workbench on {url}  (cases in {app.cases}); Ctrl+C to stop")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv
