"""Build the read-only online showcase (static site for Vercel) from a running workbench.

The real workbench UI is captured as it is: every GET it makes is saved as JSON, byte views it can ask
for (frame headers, hex windows, disk-map tiles) are precomputed, and actions that run the system
(new analysis, redaction, sealing, acquisition) answer with a notice that they need the live system.
Media are web previews: clips re-encoded smaller (same frames and timing), thumbnails recompressed;
hashes shown in the UI always refer to the original evidence and exports.

  python -m sarvadristhi ui --cases <copy of cases> --port 8790 --no-browser     # a COPY: replays write custody
  python scripts/build_showcase.py --server http://127.0.0.1:8790 --cases <copy of cases> --case OP-2013 \
         --runs cases --out site
"""
import argparse
import base64
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "sarvadristhi" / "ui_static"
ASK = ["Was anything deleted or edited?", "People getting into a vehicle", "When did a vehicle start near the parking camera?",
       "Person entering on the bus lobby camera", "Any clock changes?", "Grey cars between 16:51 and 16:52",
       "Who is the man at the stairwell?", "Was the disk formatted?", "Any disguised files?"]


class Snap:
    def __init__(self, server, out, scrub):
        self.server, self.out, self.scrub = server.rstrip("/"), Path(out), scrub

    def raw(self, path, body=None):
        req = urllib.request.Request(self.server + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            return e.read()

    def get(self, path, body=None):
        return json.loads(self.clean(self.raw(path, body).decode("utf-8")))

    def clean(self, text):
        for a, b in self.scrub:
            text = text.replace(a, b)
        return text

    def save(self, rel, obj):
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.clean(json.dumps(obj, separators=(",", ":"), default=str)), encoding="utf-8")
        return p


def build(a):
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    case, cdir = a.case, Path(a.cases) / a.case
    # local paths never reach the public site: keep them relative to the project (kit\...)
    scrub = []
    for pre in {str(Path(a.cases).resolve()), str(ROOT), *a.scrub}:
        for v in (pre + "\\", pre):
            scrub.append((json.dumps(v)[1:-1], ""))
    s = Snap(a.server, out, scrub)
    E = lambda x: urllib.parse.quote(x, safe="")
    api = f"/api/case/{E(case)}"
    # reports rendered with the current template (on the copies: regenerating is a custody entry)
    from sarvadristhi.ui import regenerate_report

    def publishable(d):
        """On a COPY only: evidence paths become project-relative, so the published report shows no local paths."""
        for p in [d / "case.json", *d.glob("evidence/*/case.json")]:
            p.write_text(s.clean(p.read_text(encoding="utf-8")), encoding="utf-8")
        regenerate_report(d)

    if a.regen:
        publishable(cdir)

    # ---------------------------------------------------------------- app shell
    html = s.raw("/").decode("utf-8")                                  # BOOT already filled in
    html = patch_app(html)
    (out / "workbench").mkdir(parents=True)
    (out / "workbench" / "index.html").write_text(html, encoding="utf-8")
    shutil.copytree(STATIC / "vendor", out / "static" / "vendor")

    # ---------------------------------------------------------------- plain GETs
    c = s.get(api)
    s.save(f"snap/api/case/{case}.json", c)
    for g in ("hypotheses", "reconcile", "capsule", "anveshan", "notes", "offsets", "segments"):
        s.save(f"snap/api/case/{case}/{g}.json", s.get(f"{api}/{g}"))
    for g in ("capabilities", "standards", "kasauti", "caselist", "sources", "cases"):
        s.save(f"snap/api/{g}.json", s.get(f"/api/{g}"))
    s.save("snap/api/drives.json", [])
    s.save("snap/api/jobs.json", [])
    (out / "snap" / "kasauti_template.csv").write_bytes(s.raw("/api/kasauti/template.csv"))

    items = [e["id"] for e in c.get("evidence_items", [])]
    U, K, app = local(a.cases)              # byte-level views: thousands of calls, computed in-process
    # disk maps, tiles and their hex windows
    hexes = {e: {} for e in items}
    for e in items:
        for bins in (2400, 600):
            s.save(f"snap/api/case/{case}/diskmap/{e}__bins{bins}.json", U.disk_map(app, case, e, bins))
        m = U.disk_map(app, case, e, 2400)
        if m.get("error"):
            continue
        bpb = m["bytes_per_bin"]
        offs = [(int(b * bpb), int((b + 1) * bpb)) for b in range(m["bins"])]
        cells = {f"{x}-{y}": U.cell_info(app, case, e, x, y) for x, y in offs}
        s.save(f"snap/api/case/{case}/cell/{e}.json", cells)
        want = {x for x, _ in offs} | {r["offset"] for r in m.get("regions", [])}
        for r in cells.values():
            want |= {g.get("first_offset") for g in r.get("clips", []) if g.get("first_offset") is not None}
        want |= {0, 0x200}
        for o in sorted(want):
            h = U.hex_view(app, case, e, o)
            if h.get("rows"):
                hexes[e][str(h["offset"])] = "".join(r["hex"].replace(" ", "") for r in h["rows"])
    for e, hx in hexes.items():
        s.save(f"snap/api/case/{case}/hexbytes/{e}.json", {"source": next((i["path"] for i in c["evidence_items"] if i["id"] == e), ""),
                                                          "rows": hx})
    # Frame X-ray: every clip's frame list and the header at each frame
    frames = {e: {} for e in items}
    for k in c.get("clips", []):
        x = K.frames(cdir, k["id"])
        s.save(f"snap/api/case/{case}/xray/{k['id']}.json", x)
        ev = x.get("evidence") or "E1"
        if x.get("mode") == "frames":
            offs = [f[1] for f in x["frames"]]
        else:                                                        # files on removable media: every 16 KiB of each run
            offs = [o for r in x.get("runs", []) for o in range(int(r[0]) // 16 * 16, int(r[0]) + int(r[1]), 16384)]
        for o in offs:
            if str(o) not in frames[ev]:
                h = K.header(cdir, ev, o)
                if not h.get("error"):
                    frames[ev][str(o)] = [h["hex"], h.get("fields", [])]
    for e, fr in frames.items():
        s.save(f"snap/api/case/{case}/frame/{e}.json", fr)
    # person search for every subject in the picker
    for cand in s.get(f"{api}/anveshan").get("candidates", []):
        s.save(f"snap/api/case/{case}/anveshan/{cand['track']}.json", s.get(f"{api}/anveshan/{E(cand['track'])}"))
    # ask the case: the example questions
    s.save(f"snap/api/case/{case}/ask.json", {q.lower(): s.get(f"{api}/ask", {"q": q}) for q in ASK})
    # replay: clean, and each simulated flipped byte (run on the COPY: replays are logged to its custody)
    cap = s.get(f"{api}/capsule")
    if cap.get("exists"):
        rep = {"": s.get(f"{api}/replay", {"tamper": ""})}
        for cid in cap["clip_ids"]:
            rep[cid] = s.get(f"{api}/replay", {"tamper": cid})
        s.save(f"snap/api/case/{case}/replay.json", rep)

    # ---------------------------------------------------------------- media (web previews)
    fdir = out / "case" / case / "file"
    for k in c.get("clips", []):
        if k.get("mp4_file"):
            src, dst = cdir / "clips" / k["mp4_file"], fdir / "clips" / k["mp4_file"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "31",
                            "-vf", "scale='min(854,iw)':-2", "-pix_fmt", "yuv420p", "-an", "-fps_mode", "passthrough",
                            "-movflags", "+faststart", "-threads", "1", str(dst)], check=True)
            for t in (1, 2, 3, 4):
                img = out / "snap" / "still" / case / f"{k['mp4_file']}__t{t}.jpg"
                img.parent.mkdir(parents=True, exist_ok=True)
                img.write_bytes(s.raw(f"{api}/still/{E(k['mp4_file'])}?t={t}"))
    for p in (cdir / "ai" / "thumbs").glob("*.jpg"):
        im = cv2.imread(str(p))
        h, w = im.shape[:2]
        if w > 480:
            im = cv2.resize(im, (480, int(h * 480 / w)), interpolation=cv2.INTER_AREA)
        d = fdir / "ai" / "thumbs" / p.name
        d.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d), im, [cv2.IMWRITE_JPEG_QUALITY, 62])
    for rel in ["report.pdf", "report.html", *[str(p.relative_to(cdir)).replace("\\", "/") for p in (cdir / "report_frames").glob("*")],
                *[str(p.relative_to(cdir)).replace("\\", "/") for p in (cdir / "evidence").glob("E*/frames.csv")]]:
        if (cdir / rel).exists():
            (fdir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cdir / rel, fdir / rel)

    # ---------------------------------------------------------------- every finished run: summary + reports
    runs = []
    for d in sorted(Path(a.runs).glob("OP-*")):
        cj = d / "case.json"
        if not cj.exists():
            continue
        if d.name == case:
            d = cdir
        elif a.regen:                                                # regenerate on a copy, never on the original
            w = Path(a.regen) / d.name
            if not w.exists():
                shutil.copytree(d, w)
            publishable(w)
            d = w
        r = json.loads(cj.read_text(encoding="utf-8"))
        po = r.get("poison_scorecard") or []
        from sarvadristhi.custody import Ledger
        ok, status = Ledger(d / "custody.jsonl").verify()
        runs.append({"id": d.name, "examiner": r.get("case", {}).get("examiner"),
                     "items": [f"{e['name']} · {e.get('family')}" for e in r.get("evidence_items", [])],
                     "frames": sum(e.get("frames") or 0 for e in r.get("evidence_items", [])), "clips": len(r.get("clips", [])),
                     "findings": len(r.get("findings", [])), "critical": sum(f.get("severity") == "critical" for f in r.get("findings", [])),
                     "caught": sum(bool(p.get("caught")) for p in po), "planted": len(po),
                     "unchanged": (r.get("evidence") or {}).get("unchanged"), "custody": status, "custody_ok": ok,
                     "explore": d.name == case})
        for f in ("report.pdf",):
            if (d / f).exists():
                (out / "runs" / d.name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(d / f, out / "runs" / d.name / f)
    s.save("runs.json", runs)
    shutil.copy2(Path(__file__).with_name("showcase_index.html"), out / "index.html")
    (out / "vercel.json").write_text(json.dumps({
        "cleanUrls": False, "trailingSlash": True,
        "headers": [{"source": "/(.*)\\.mp4", "headers": [{"key": "Cache-Control", "value": "public, max-age=86400"}]},
                    {"source": "/snap/(.*)", "headers": [{"key": "Cache-Control", "value": "public, max-age=3600"}]}]}, indent=1))
    print(f"showcase written to {out}")


def local(cases):
    """The workbench's own functions, in-process, with large case files read and parsed once."""
    import pathlib
    from sarvadristhi import kanika as K
    from sarvadristhi import ui as U
    rt, loads, texts, parsed = pathlib.Path.read_text, json.loads, {}, {}

    def read_text(self, *a, **k):
        try:
            st = self.stat()
        except OSError:
            return rt(self, *a, **k)
        if st.st_size < 1_000_000:
            return rt(self, *a, **k)
        key = (str(self), st.st_mtime_ns)
        if key not in texts:
            texts[key] = rt(self, *a, **k)
        return texts[key]

    def jloads(s, *a, **k):
        if isinstance(s, str) and len(s) >= 1_000_000 and any(s is t for t in texts.values()):
            if id(s) not in parsed:
                parsed[id(s)] = loads(s, *a, **k)
            return parsed[id(s)]
        return loads(s, *a, **k)

    pathlib.Path.read_text = read_text
    json.loads = jloads
    return U, K, U.App(Path(cases))


SHIM = r"""<script>
/* Online showcase: the real workbench, served from saved data. Actions that run the system are disabled. */
window.SHOWCASE=true;
const GPU_MSG="Read-only showcase. This action runs on the live system (Windows laptop with an NVIDIA RTX 4050 GPU). Every recorded run, report and result is here to explore.";
(function(){const real=window.fetch.bind(window),cache={};
 const J=o=>new Response(JSON.stringify(o),{status:200,headers:{"Content-Type":"application/json"}});
 const load=async u=>{if(!(u in cache))cache[u]=real(u).then(r=>r.ok?r.json():null).catch(()=>null);return cache[u]};
 const dec=s=>decodeURIComponent(s);
 window.fetch=async function(input,opt){const url=typeof input==="string"?input:input.url;
  if(!url.startsWith("/api/"))return real(input,opt);
  const u=new URL(url,location.origin),P=u.pathname.split("/").filter(Boolean).map(dec),q=u.searchParams,m=((opt&&opt.method)||"GET").toUpperCase();
  const base=`/snap/api/case/${encodeURIComponent(P[2]||"")}`;
  if(P[1]==="case"&&P.length>=4){const kind=P[3];
   if(kind==="ask"){const b=JSON.parse((opt&&opt.body)||"{}"),A=await load(base+"/ask.json")||{};const hit=A[String(b.q||"").trim().toLowerCase()];
    return J(hit||{question:b.q,refused:true,citations:[],answer:"In this online showcase you can ask the example questions above. Free questions run on the live system (RTX 4050 GPU)."})}
   if(kind==="replay"){const b=JSON.parse((opt&&opt.body)||"{}"),R=await load(base+"/replay.json");return J((R&&R[b.tamper||""])||{error:GPU_MSG})}
   if(m!=="GET")return J({error:GPU_MSG});
   if(kind==="diskmap")return J(await load(`${base}/diskmap/${P[4]}__bins${q.get("bins")||1600}.json`)||{error:"Not in the online snapshot."});
   if(kind==="cell"){const C=await load(`${base}/cell/${P[4]}.json`)||{};return J(C[`${q.get("a")}-${q.get("b")}`]||{clips:[],structures:[],files:[]})}
   if(kind==="hex"){const H=await load(`${base}/hexbytes/${P[4]}.json`);if(!H)return J({error:"Not in the online snapshot."});
    const want=Math.floor((+q.get("offset")||0)/16)*16,keys=Object.keys(H.rows).map(Number).sort((a,b)=>a-b);
    let k=keys.filter(x=>x<=want).pop();if(k==null||want-k>=256)return J({error:`Offset 0x${want.toString(16)} is not in the online snapshot. Saved here: tile starts, structures and frames. The live system reads any offset.`});
    const hx=H.rows[k],rows=[];for(let i=0;i<hx.length/2;i+=16){const b=hx.slice(i*2,i*2+32).match(/../g)||[];
     rows.push({offset:k+i,hex:b.join(" "),ascii:b.map(x=>{const v=parseInt(x,16);return v>=32&&v<127?String.fromCharCode(v):"."}).join("")})}
    return J({offset:k,rows,source:H.source+" (saved bytes)",read_only:true})}
   if(kind==="frame"){const F=await load(`${base}/frame/${P[4]}.json`)||{};const o=+q.get("offset")||0;let k=String(o);
    if(!F[k]){const ks=Object.keys(F).map(Number).sort((a,b)=>a-b);const lo=ks.filter(x=>x<=o).pop();if(lo==null)return J({error:"Not in the online snapshot."});k=String(lo)}
    return J({offset:+k,hex:F[k][0],fields:F[k][1],read_only:true})}
   if(kind==="anveshan"&&P[4]){let f=await load(`${base}/anveshan/${encodeURIComponent(P[4])}.json`);
    if(!f){const L=await load(base+"/anveshan.json");const c=(L&&L.candidates||[]).find(c=>(c.members||[]).includes(P[4]));if(c)f=await load(`${base}/anveshan/${encodeURIComponent(c.track)}.json`)}
    return J(f||{error:"Not in the online snapshot."})}
   if(kind==="xray")return J(await load(`${base}/xray/${encodeURIComponent(P[4])}.json`)||{error:"Not in the online snapshot."})}
  if(m!=="GET")return J({error:GPU_MSG});
  if(P[1]==="job")return J({error:GPU_MSG});
  return J(await load("/snap"+u.pathname.split("/").map(x=>x).join("/")+".json")||{error:"Not in the online snapshot."})};
 const orig=window.toast;window.addEventListener("DOMContentLoaded",()=>{const b=document.createElement("a");b.href="/";b.className="showcase-pill";
  b.innerHTML="<b>Read-only showcase</b> · live analysis needs an RTX 4050 GPU · ← home";document.body.appendChild(b)})})();
</script>
<style>.showcase-pill{position:fixed;right:16px;bottom:16px;z-index:9999;background:#0f1c17;color:#bdf5d3;border:1px solid #2a4a3c;border-radius:999px;
 padding:8px 14px;font:500 12.5px/1.2 system-ui,sans-serif;text-decoration:none;box-shadow:0 6px 24px rgba(0,0,0,.25)}.showcase-pill b{color:#7fe3a6}</style>"""


def patch_app(html):
    reps = [
        ('src="/api/case/${encodeURIComponent(c.dir)}/still/${encodeURIComponent(c.cover)}?t=3"',
         'src="/snap/still/${encodeURIComponent(c.dir)}/${encodeURIComponent(c.cover)}__t3.jpg"'),
        ("`/api/case/${encodeURIComponent(CDIR)}/still/${encodeURIComponent(k.mp4_file)}?t=${t}`",
         "`/snap/still/${encodeURIComponent(CDIR)}/${encodeURIComponent(k.mp4_file)}__t${t}.jpg`"),
        ('href="/api/kasauti/template.csv"', 'href="/snap/kasauti_template.csv"'),
        # the web copy says what it is: a saved run, not this machine
        ("<div>Runs on<b>127.0.0.1 · offline</b></div>", "<div>This site<b>read-only showcase</b></div><div>Live system<b>RTX 4050 laptop</b></div>"),
        ("Local only · nothing leaves this PC", "Read-only showcase · the live system runs on an RTX 4050 laptop"),
        ("on this workstation</span>", "recorded on the RTX 4050 laptop</span>"),
        ("<title>", SHIM + "\n<title>"),
    ]
    for x, y in reps:
        assert x in html, x[:70]
        html = html.replace(x, y, 1)
    return html


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8790")
    ap.add_argument("--cases", required=True, help="the case folder the server was started on (a copy)")
    ap.add_argument("--case", default="OP-2013")
    ap.add_argument("--runs", required=True, help="folder whose finished runs are listed with their reports")
    ap.add_argument("--out", default="site")
    ap.add_argument("--regen", help="work folder: re-render every run's report there (copies) with the current template")
    ap.add_argument("--scrub", action="append", default=[], help="a local path prefix to strip from the published data")
    build(ap.parse_args())
