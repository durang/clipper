#!/usr/bin/env python3
"""Clipper Studio - interfaz web para clipper."""
from __future__ import annotations
import json, os, re, shutil, subprocess, threading, uuid, zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(os.environ.get("STUDIO_PORT", "8791"))
HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")
ROOT = Path(os.environ.get("STUDIO_DIR", str(Path.home() / "clipper-studio")))
CLIPPER = Path(os.environ.get("CLIPPER", str(Path(__file__).with_name("clipper.py"))))
PYTHON = os.environ.get("STUDIO_PYTHON", "python3")
WHISPER = Path(os.environ.get("WHISPER_BIN", str(Path.home() / ".local/bin/whisper")))
MAX_BYTES = int(os.environ.get("STUDIO_MAX_BYTES", str(4 * 1024**3)))
JOBS = ROOT / "jobs"; DICT_FILE = ROOT / "dictionary.json"
# clipper.py aplica el diccionario (con límite de palabra y correcciones de varias palabras
# también en los tiempos por palabra); le decimos cuál es el de Studio.
os.environ.setdefault("CLIPPER_DICT", str(DICT_FILE))
JOBS.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock(); RUNNING: dict = {}
PAGE_FILE = Path(__file__).with_name("clipper-studio.html")

FORMATS = {
 "vertical":   {"args": [], "caption": 1.0},
 "horizontal": {"args": ["--horizontal"], "caption": 1.4},
 "youtube":    {"args": ["--horizontal", "--out-height", "1080"], "caption": 1.7},
}

def safe_slug(raw, fallback="clip"):
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (raw or "").strip()).strip("-.")
    return (s or fallback)[:80]

def load_dict():
    try: return json.loads(DICT_FILE.read_text("utf-8"))
    except Exception: return {}

def save_dict(d): DICT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")

def job_dir(jid):
    d = (JOBS / safe_slug(jid, "x")).resolve()
    d.relative_to(JOBS.resolve())
    return d

def read_job(jid):
    f = job_dir(jid) / "job.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else None

def write_job(job):
    with LOCK:
        d = job_dir(job["id"]); d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")

def log(job, msg):
    job.setdefault("log", []).append(f"{datetime.now():%H:%M:%S}  {msg}")
    job["log"] = job["log"][-200:]; write_job(job)

def run(cmd, job, cwd=None):
    log(job, "$ " + " ".join(str(c) for c in cmd[:9]))
    p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        line = line.rstrip()
        if line: log(job, "   " + line[:200])
    p.wait(); return p.returncode

def do_analyze(jid, lang, model):
    job = read_job(jid)
    if not job: return
    try:
        job["status"]="analizando"; job["step"]="transcribir"; write_job(job)
        d = job_dir(jid); video = d / job["video"]
        # "auto" lo resuelve clipper: detecta con tiny y, si no puede, falla — nunca asume español.
        rc = run([PYTHON, CLIPPER, "analyze", video.name, "--model", model,
                  "--lang", lang or "auto", "--force"], job, cwd=d)
        cands = list(d.glob("*.transcript.json"))
        if rc != 0 or not cands:
            job["status"]="error"; log(job, f"analyze fallo (rc={rc})")
        else:
            tj = cands[0]; data = json.loads(tj.read_text("utf-8"))
            job["lang"] = data.get("language", lang)
            job["transcript"]=tj.name; job["status"]="transcrito"; job["step"]="revisar"
            log(job, f"listo: {len(data.get('segments',[]))} segmentos")
        write_job(job)
    except Exception as e:
        job = read_job(jid) or job; job["status"]="error"; log(job, f"excepcion: {e}")
    finally:
        RUNNING.pop(jid, None)

def do_render(jid, cfg):
    job = read_job(jid)
    if not job: return
    try:
        job["status"]="renderizando"; job["step"]="render"; job["outputs"]=[]; write_job(job)
        d = job_dir(jid)
        (d/"clips.json").write_text(json.dumps({"clips":cfg["clips"]}, ensure_ascii=False, indent=2),"utf-8")
        wm = []
        if job.get("watermark"):
            wm = ["--watermark", job["watermark"], "--watermark-scale", str(cfg.get("watermark_scale",0.16))]
        outdirs=[]
        for key in cfg["formats"]:
            spec = FORMATS.get(key)
            if not spec: continue
            od = d/"out"/key; od.mkdir(parents=True, exist_ok=True)
            cs = cfg.get("caption_scales",{}).get(key, spec["caption"])
            cmd = [PYTHON, CLIPPER, "render", job["transcript"], "clips.json", *spec["args"], *wm,
                   "--caption-scale", str(cs), "--words-per-caption", str(cfg.get("wpc",3)),
                   "--crf", str(cfg.get("crf",19)), "--preset", cfg.get("preset","medium"),
                   "--outdir", str(od)]
            log(job, f"--- formato {key} ---")
            if run(cmd, job, cwd=d) == 0: outdirs.append(od)
            else: log(job, f"formato {key} fallo")
        outs=[]
        for od in outdirs:
            for f in sorted(od.glob("*.mp4")):
                outs.append({"rel":str(f.relative_to(d)),"name":f.name,"fmt":od.name,
                             "mb":round(f.stat().st_size/1048576,2)})
        if outs:
            zp = d/f"{safe_slug(job['name'],'clips')}-clips.zip"
            with zipfile.ZipFile(zp,"w",zipfile.ZIP_STORED) as z:
                for o in outs: z.write(d/o["rel"], o["rel"])
            job["zip"]=zp.name
        job["outputs"]=outs; job["status"]="listo" if outs else "error"; job["step"]="descargar"
        log(job, f"terminado: {len(outs)} archivos"); write_job(job)
    except Exception as e:
        job = read_job(jid) or job; job["status"]="error"; log(job, f"excepcion: {e}")
    finally:
        RUNNING.pop(jid, None)

def spawn(jid, fn, *a):
    if RUNNING.get(jid): return False
    RUNNING[jid]=True
    threading.Thread(target=fn, args=(jid,*a), daemon=True).start()
    return True

class H(BaseHTTPRequestHandler):
    server_version = "ClipperStudio/1.0"
    def log_message(self, fmt, *args): print(f"{self.address_string()} {fmt % args}")
    def send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options","nosniff"); self.end_headers()
        try: self.wfile.write(body)
        except BrokenPipeError: pass
    def js(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES: raise ValueError("demasiado grande")
        buf = bytearray(); left = n
        while left > 0:
            c = self.rfile.read(min(1<<20, left))
            if not c: break
            buf += c; left -= len(c)
        return bytes(buf)
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        if p in ("/","/index.html"):
            return self.send(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        if p == "/api/health":
            return self.js({"ok":True,"clipper":CLIPPER.exists(),"jobs":len(list(JOBS.iterdir()))})
        if p == "/api/dictionary": return self.js(load_dict())
        m = re.match(r"^/api/job/([\w.-]+)$", p)
        if m:
            j = read_job(m.group(1)); return self.js(j or {"error":"no existe"}, 200 if j else 404)
        m = re.match(r"^/api/job/([\w.-]+)/transcript$", p)
        if m:
            j = read_job(m.group(1))
            if not j or not j.get("transcript"): return self.js({"error":"sin transcripcion"},404)
            return self.js(json.loads((job_dir(j["id"])/j["transcript"]).read_text("utf-8")))
        m = re.match(r"^/api/job/([\w.-]+)/file$", p)
        if m:
            j = read_job(m.group(1)); rel = unquote(q.get("p",[""])[0])
            if not j or not rel: return self.js({"error":"falta p"},400)
            d = job_dir(j["id"])
            try:
                f = (d/rel).resolve(); f.relative_to(d.resolve())
            except Exception: return self.js({"error":"ruta invalida"},400)
            if not f.is_file(): return self.js({"error":"no existe"},404)
            self.send_response(200)
            self.send_header("Content-Type","application/octet-stream")
            self.send_header("Content-Length",str(f.stat().st_size))
            self.send_header("Content-Disposition",f'attachment; filename="{f.name}"')
            self.end_headers()
            with f.open("rb") as fh: shutil.copyfileobj(fh, self.wfile, 1<<20)
            return
        return self.js({"error":"ruta desconocida"},404)
    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/upload":
                name = safe_slug(unquote(self.headers.get("X-Filename","video.mp4")),"video.mp4")
                if not re.search(r"\.(mp4|mov|mkv|webm|m4v|avi)$", name, re.I): name += ".mp4"
                data = self.body()
                if not data: return self.js({"error":"sin datos"},400)
                jid = datetime.now().strftime("%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:6]
                d = job_dir(jid); d.mkdir(parents=True, exist_ok=True)
                (d/name).write_bytes(data)
                job = {"id":jid,"name":Path(name).stem,"video":name,"status":"subido",
                       "step":"transcribir","created":datetime.now().isoformat(timespec="seconds"),
                       "mb":round(len(data)/1048576,2),"log":[]}
                write_job(job); log(job, f"subido {name} ({job['mb']} MB)")
                return self.js(job)
            m = re.match(r"^/api/job/([\w.-]+)/watermark$", p)
            if m:
                j = read_job(m.group(1))
                if not j: return self.js({"error":"no existe"},404)
                name = safe_slug(unquote(self.headers.get("X-Filename","logo.png")),"logo.png")
                (job_dir(j["id"])/name).write_bytes(self.body())
                j["watermark"]=name; log(j,f"logo cargado: {name}"); return self.js(j)
            payload = json.loads(self.body() or b"{}")
            m = re.match(r"^/api/job/([\w.-]+)/analyze$", p)
            if m:
                j = read_job(m.group(1))
                if not j: return self.js({"error":"no existe"},404)
                return self.js({"started":spawn(j["id"],do_analyze,payload.get("lang","auto"),payload.get("model","base"))})
            m = re.match(r"^/api/job/([\w.-]+)/transcript$", p)
            if m:
                j = read_job(m.group(1))
                if not j or not j.get("transcript"): return self.js({"error":"sin transcripcion"},404)
                t = job_dir(j["id"])/j["transcript"]; data = json.loads(t.read_text("utf-8"))
                for i,txt in (payload.get("edits") or {}).items():
                    k = int(i)
                    if 0 <= k < len(data["segments"]): data["segments"][k]["text"]=txt
                t.write_text(json.dumps(data, ensure_ascii=False, indent=2),"utf-8")
                log(j, f"subtitulos editados ({len(payload.get('edits') or {})})")
                return self.js({"ok":True})
            m = re.match(r"^/api/job/([\w.-]+)/render$", p)
            if m:
                j = read_job(m.group(1))
                if not j: return self.js({"error":"no existe"},404)
                if not payload.get("clips"): return self.js({"error":"sin momentos"},400)
                return self.js({"started":spawn(j["id"],do_render,payload)})
            if p == "/api/dictionary":
                d = load_dict(); d.update({k:v for k,v in (payload or {}).items() if k and v and not k.startswith("_")})
                for k in payload.get("_delete",[]): d.pop(k,None)
                save_dict(d); return self.js(d)
            return self.js({"error":"ruta desconocida"},404)
        except Exception as e:
            return self.js({"error":str(e)},500)

def main():
    if not CLIPPER.exists(): print(f"AVISO: no encuentro clipper en {CLIPPER}")
    print(f"Clipper Studio en http://{HOST}:{PORT}  ·  trabajos: {JOBS}")
    ThreadingHTTPServer((HOST,PORT), H).serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
