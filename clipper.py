#!/usr/bin/env python3
"""
clipper — de video largo a clips verticales listos para publicar.

Diseño en fases, a propósito:

  0. `fetch`    → opcional: baja el video de YouTube o cualquier sitio soportado.
  1. `analyze`  → trabajo de máquina: transcribe con marcas de tiempo por palabra.
  2. (criterio) → un humano o un agente lee la transcripción y elige los momentos.
  3. `render`   → trabajo de máquina: corta, reencuadra, subtitula y normaliza.

La fase 2 NO se automatiza con heurísticas de silencio. Elegir qué momento vale
la pena es criterio, y el criterio se delega a quien tiene contexto.

Requisitos: ffmpeg (con libass), whisper. Opcional: yt-dlp para `fetch`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

WHISPER = os.environ.get("CLIPPER_WHISPER", "whisper")
FFMPEG = os.environ.get("CLIPPER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("CLIPPER_FFPROBE", "ffprobe")
YTDLP = os.environ.get("CLIPPER_YTDLP", "yt-dlp")

# Límites de duración por plataforma, en segundos.
PLATFORMS = {
    "reels": ("Instagram Reels", 90),
    "shorts": ("YouTube Shorts", 180),
    "tiktok": ("TikTok", 600),
    "x": ("X / Twitter", 140),
}


# ---------------------------------------------------------------- utilidades

def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def need(binary: str):
    if shutil.which(binary) is None:
        die(f"no encontré '{binary}' en el PATH")


def run(cmd: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=False,
    )


def duration_of(path: Path) -> float:
    p = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def font_name() -> str:
    """Devuelve una familia de fuente que exista en el sistema."""
    for candidate in ("Noto Sans", "DejaVu Sans", "Liberation Sans", "Arial"):
        p = subprocess.run(["fc-match", candidate, "-f", "%{family}"],
                           capture_output=True, text=True, check=False)
        got = (p.stdout or "").strip()
        if got and candidate.split()[0].lower() in got.lower():
            return got.split(",")[0]
    return "sans-serif"


FONT = font_name()


def video_fingerprint(path: Path) -> str:
    """Huella rápida: tamaño + primeros y últimos 1 MB. Evita leer archivos enormes."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read(1024 * 1024))
    return h.hexdigest()[:16]


def ass_time(seconds: float) -> str:
    s = max(0.0, seconds)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        cs, sec = 0, sec + 1
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").strip()


# ---------------------------------------------------------------- fetch

def cmd_fetch(args) -> int:
    """Baja un video de YouTube, Reels, TikTok, X o cualquier sitio de yt-dlp."""
    need(YTDLP)
    outdir = Path(args.outdir).expanduser().resolve() if args.outdir else Path.cwd()
    outdir.mkdir(parents=True, exist_ok=True)

    tmpl = str(outdir / "%(title).80s-%(id)s.%(ext)s")
    cmd = [YTDLP, "--no-playlist", "--restrict-filenames",
           "--merge-output-format", "mp4", "-o", tmpl]

    if args.cookies_from_browser:
        cmd += ["--cookies-from-browser", args.cookies_from_browser]
    if args.cookies:
        cmd += ["--cookies", str(Path(args.cookies).expanduser())]

    cmd += ["-f", args.format or
            "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
            "bv*[height<=1080]+ba/b[height<=1080]/b"]
    cmd += ["--print", "after_move:filepath", args.url]

    print(f"bajando {args.url}")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)

    path = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line and Path(line).exists():
            path = Path(line)

    if p.returncode != 0 or path is None:
        err = (p.stderr or "").strip().splitlines()
        print("  no se pudo bajar:", file=sys.stderr)
        for l in err[-6:]:
            print(f"    {l}", file=sys.stderr)
        if any("sign in" in l.lower() or "bot" in l.lower() for l in err):
            print("\n  YouTube bloquea IPs de centro de datos. Opciones:", file=sys.stderr)
            print("    --cookies-from-browser chrome   (desde tu máquina)", file=sys.stderr)
            print("    --cookies cookies.txt           (exportadas del navegador)", file=sys.stderr)
        return 1

    print(f"  {path.name}")
    print(f"  {duration_of(path)/60:.1f} min · {path.stat().st_size/1e6:.1f} MB")

    if args.analyze:
        print()
        ns = argparse.Namespace(video=str(path), model=args.model,
                                lang=args.lang, out=None, words=True, force=False)
        return cmd_analyze(ns)

    print(f"\nsigue:  clipper.py analyze '{path.name}'")
    return 0


# ---------------------------------------------------------------- analyze

@dataclass
class Segment:
    idx: int
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)


def transcribe(video: Path, workdir: Path, model: str, lang: str,
               words: bool) -> list[Segment]:
    wav = workdir / "audio.wav"
    print("  extrayendo audio…", flush=True)
    rc = run([FFMPEG, "-y", "-i", str(video), "-ar", "16000", "-ac", "1",
              "-c:a", "pcm_s16le", str(wav)])
    if rc.returncode != 0 or not wav.exists():
        die("ffmpeg no pudo extraer el audio")

    cmd = [WHISPER, str(wav), "--model", model, "--language", lang,
           "--task", "transcribe", "--output_format", "json",
           "--output_dir", str(workdir), "--fp16", "False"]
    if words:
        cmd += ["--word_timestamps", "True"]

    print(f"  transcribiendo (modelo {model}"
          f"{', palabra por palabra' if words else ''})… esto tarda", flush=True)
    run(cmd)

    js = workdir / "audio.json"
    if not js.exists():
        die("whisper no produjo transcripción")

    data = json.loads(js.read_text(encoding="utf-8"))
    segs = []
    for i, s in enumerate(data.get("segments", []), start=1):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        ws = []
        for w in (s.get("words") or []):
            t = (w.get("word") or "").strip()
            if not t:
                continue
            try:
                ws.append({"w": t, "start": float(w["start"]), "end": float(w["end"])})
            except (KeyError, TypeError, ValueError):
                continue
        segs.append(Segment(i, float(s["start"]), float(s["end"]), txt, ws))
    return segs


def cmd_analyze(args) -> int:
    need(FFMPEG); need(FFPROBE); need(WHISPER)
    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        die(f"no existe {video}")

    out = Path(args.out).expanduser().resolve() if args.out else \
        video.parent / f"{video.stem}.transcript.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    fp = video_fingerprint(video)

    # Caché: si ya transcribimos este mismo archivo, no repetimos whisper.
    if out.exists() and not args.force:
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            if prev.get("fingerprint") == fp:
                n = prev.get("segment_count", 0)
                has_w = any(s.get("words") for s in prev.get("segments", []))
                if not args.words or has_w:
                    print(f"transcripción en caché ({n} segmentos)")
                    print(f"  {out}")
                    print("  usa --force para rehacerla")
                    return 0
        except (json.JSONDecodeError, OSError):
            pass

    total = duration_of(video)
    print(f"analizando {video.name}  ({total/60:.1f} min)")

    with tempfile.TemporaryDirectory(prefix="clipper-") as td:
        segs = transcribe(video, Path(td), args.model, args.lang, args.words)

    if not segs:
        die("la transcripción salió vacía")

    word_total = sum(len(s.words) for s in segs)
    payload = {
        "source": str(video),
        "fingerprint": fp,
        "duration_sec": round(total, 2),
        "model": args.model,
        "language": args.lang,
        "segment_count": len(segs),
        "word_count": word_total,
        "segments": [asdict(s) for s in segs],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    readable = out.with_suffix(".txt")
    lines = [f"# {video.name} · {total/60:.1f} min · {len(segs)} segmentos", ""]
    for s in segs:
        lines.append(f"[{s.start:7.1f} → {s.end:7.1f}]  {s.text}")
    readable.write_text("\n".join(lines), encoding="utf-8")

    print("\nlisto:")
    print(f"  {out}")
    print(f"  {readable}   ← pásame este archivo para que elija los momentos")
    print(f"\n{len(segs)} segmentos"
          f"{f', {word_total} palabras con tiempo' if word_total else ''}"
          f", {total/60:.1f} minutos de material.")
    return 0


# ---------------------------------------------------------------- subtítulos

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,7,0,2,60,60,{cap_margin},1
Style: Hook,{font},{hook_size},&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,0,8,70,70,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def chunk_words(words: list[dict], start: float, end: float,
                per_chunk: int) -> list[tuple[float, float, str]]:
    """Agrupa palabras en bloques cortos — el estilo que domina en Reels."""
    inside = [w for w in words if w["end"] > start and w["start"] < end]
    out = []
    for i in range(0, len(inside), per_chunk):
        grp = inside[i:i + per_chunk]
        a = max(grp[0]["start"], start) - start
        b = min(grp[-1]["end"], end) - start
        if b - a < 0.08:
            b = a + 0.08
        text = " ".join(w["w"] for w in grp)
        out.append((a, b, text))
    return out


def build_ass(segs: list[dict], start: float, end: float, vertical: bool,
              hook: str | None, per_chunk: int) -> str:
    cap_size = 96 if vertical else 54
    hook_size = 76 if vertical else 44
    body = ASS_HEADER.format(font=FONT, cap_size=cap_size, hook_size=hook_size,
                             cap_margin=260 if vertical else 90)
    events = []

    all_words = []
    for s in segs:
        all_words.extend(s.get("words") or [])

    if all_words:
        for a, b, text in chunk_words(all_words, start, end, per_chunk):
            events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,"
                          f"{ass_escape(text)}")
    else:
        # Sin tiempos por palabra: caemos a subtítulo por segmento.
        for s in segs:
            s0, s1 = float(s["start"]), float(s["end"])
            if s1 <= start or s0 >= end:
                continue
            a = max(s0, start) - start
            b = min(s1, end) - start
            if b - a < 0.05:
                continue
            events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,"
                          f"{ass_escape(s['text'])}")

    if hook:
        h = ass_escape(hook)
        events.insert(0, f"Dialogue: 1,{ass_time(0)},{ass_time(3.0)},Hook,,0,0,0,,{h}")

    return body + "\n".join(events) + "\n"


# ---------------------------------------------------------------- render

def check_platform(dur: float) -> list[str]:
    warns = []
    for _, (label, limit) in PLATFORMS.items():
        if dur > limit:
            warns.append(f"{label} (máx {limit}s)")
    return warns


def watermark_chain(inlabel: str, vertical: bool, pos: str, scale: float,
                    shadow: bool) -> str:
    """Compone el logo sobre el video.

    Un logo blanco sobre fondo claro desaparece. Por eso, si `shadow` está
    activo, primero se pinta una copia ennegrecida y desenfocada del propio
    logo, desplazada unos pixeles. Da contorno sin ensuciar la marca.
    """
    fw = 1080 if vertical else 1280
    w = int(fw * scale)
    m = int(fw * 0.045)          # margen proporcional al cuadro

    xy = {
        "top-right": (f"W-w-{m}", f"{m}"),
        "top-left": (f"{m}", f"{m}"),
        "bottom-right": (f"W-w-{m}", f"H-h-{m}"),
        "bottom-left": (f"{m}", f"H-h-{m}"),
    }.get(pos, (f"W-w-{m}", f"{m}"))
    x, y = xy

    if not shadow:
        return (f";[1:v]scale={w}:-1[wm];"
                f"[{inlabel}][wm]overlay={x}:{y}[vo]")

    off = max(2, w // 90)
    return (
        f";[1:v]scale={w}:-1,split=2[wmf][wms];"
        f"[wms]colorchannelmixer=rr=0:rg=0:rb=0:gr=0:gg=0:gb=0:br=0:bg=0:bb=0,"
        f"boxblur=4:1[wsh];"
        f"[{inlabel}][wsh]overlay={x}+{off}:{y}+{off}[wbg];"
        f"[wbg][wmf]overlay={x}:{y}[vo]"
    )


def render_clip(video: Path, segs: list[dict], clip: dict, outdir: Path,
                vertical: bool, idx: int, per_chunk: int,
                normalize: bool, watermark: Path | None = None,
                wm_pos: str = "top-right", wm_scale: float = 0.22,
                wm_shadow: bool = True) -> Path | None:
    start, end = float(clip["start"]), float(clip["end"])
    if end <= start:
        print(f"  clip {idx}: rango inválido, lo salto")
        return None

    slug = (clip.get("slug") or f"clip{idx:02d}").strip().replace(" ", "-")[:48]
    dur = end - start
    out = outdir / f"{idx:02d}-{slug}.mp4"
    hook = clip.get("hook")

    with tempfile.TemporaryDirectory(prefix="clipper-r-") as td:
        td = Path(td)
        ass = td / "s.ass"
        ass.write_text(build_ass(segs, start, end, vertical, hook, per_chunk),
                       encoding="utf-8")

        tail = "vo" if not watermark else "vsub"
        if vertical:
            vf = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=22[bgb];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v];[v]ass={ass.name}[{tail}]"
            )
        else:
            vf = f"[0:v]ass={ass.name}[{tail}]"

        if watermark:
            vf += watermark_chain(tail, vertical, wm_pos, wm_scale, wm_shadow)

        cmd = [FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
               "-i", str(video)]
        if watermark:
            cmd += ["-i", str(watermark)]
        cmd += ["-filter_complex", vf, "-map", "[vo]", "-map", "0:a?"]
        if normalize:
            cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "21",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out)]

        rc = subprocess.run(cmd, cwd=td, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, check=False)

    if rc.returncode != 0 or not out.exists():
        print(f"  clip {idx}: FALLÓ")
        for l in (rc.stderr or "").strip().splitlines()[-3:]:
            print(f"      {l}")
        return None

    note = ""
    warns = check_platform(dur)
    if warns:
        note = f"  ⚠ excede {', '.join(warns)}"
    print(f"  clip {idx}: {out.name}  ({dur:.1f}s, {out.stat().st_size/1e6:.1f} MB){note}")
    return out


def cmd_render(args) -> int:
    need(FFMPEG)
    tpath = Path(args.transcript).expanduser().resolve()
    if not tpath.exists():
        die(f"no existe {tpath}")
    tdata = json.loads(tpath.read_text(encoding="utf-8"))
    segs = tdata["segments"]

    video = Path(args.video).expanduser().resolve() if args.video \
        else Path(tdata["source"])
    if not video.exists():
        die(f"no existe el video {video}")

    cpath = Path(args.clips).expanduser().resolve()
    if not cpath.exists():
        die(f"no existe {cpath}")
    clips = json.loads(cpath.read_text(encoding="utf-8"))
    if isinstance(clips, dict):
        clips = clips.get("clips", [])
    if not clips:
        die("el archivo de clips está vacío")

    outdir = Path(args.outdir).expanduser().resolve() if args.outdir \
        else video.parent / f"{video.stem}-clips"
    outdir.mkdir(parents=True, exist_ok=True)

    wm = None
    if args.watermark:
        wm = Path(args.watermark).expanduser().resolve()
        if not wm.exists():
            die(f"no existe la marca de agua {wm}")

    has_words = any(s.get("words") for s in segs)
    print(f"renderizando {len(clips)} clip(s) de {video.name}")
    print(f"  formato: {'vertical 1080x1920' if not args.horizontal else 'original'}")
    print(f"  subtítulos: {'palabra por palabra' if has_words else 'por segmento'}"
          f" · fuente {FONT}")
    print(f"  audio: {'normalizado EBU R128' if not args.no_normalize else 'sin tocar'}")
    if wm:
        print(f"  marca de agua: {wm.name} · {args.watermark_pos} · "
              f"{int(args.watermark_scale*100)}% del ancho"
              f"{' con sombra' if not args.no_watermark_shadow else ''}")
    print()

    made = []
    for i, c in enumerate(clips, start=1):
        r = render_clip(video, segs, c, outdir, not args.horizontal, i,
                        args.words_per_caption, not args.no_normalize,
                        wm, args.watermark_pos, args.watermark_scale,
                        not args.no_watermark_shadow)
        if r:
            made.append(r)

    print(f"\n{len(made)}/{len(clips)} listos en:\n  {outdir}")
    return 0 if made else 1


# ---------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="clipper",
        description="Video largo → clips verticales listos para publicar.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="baja video de YouTube y otros sitios")
    f.add_argument("url")
    f.add_argument("--outdir")
    f.add_argument("--format", help="selector de formato de yt-dlp")
    f.add_argument("--cookies", help="archivo cookies.txt")
    f.add_argument("--cookies-from-browser", dest="cookies_from_browser",
                   help="chrome|firefox|safari|edge")
    f.add_argument("--analyze", action="store_true",
                   help="transcribir inmediatamente después de bajar")
    f.add_argument("--model", default="base")
    f.add_argument("--lang", default="Spanish")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("analyze", help="transcribe con marcas de tiempo")
    a.add_argument("video")
    a.add_argument("--model", default="base",
                   help="tiny|base|small|medium (default: base)")
    a.add_argument("--lang", default="Spanish")
    a.add_argument("--out", help="ruta del .transcript.json")
    a.add_argument("--no-words", dest="words", action="store_false",
                   help="sin tiempos por palabra (más rápido)")
    a.add_argument("--force", action="store_true",
                   help="ignorar la caché y rehacer la transcripción")
    a.set_defaults(func=cmd_analyze, words=True)

    r = sub.add_parser("render", help="corta, subtitula y normaliza")
    r.add_argument("transcript", help="el .transcript.json de analyze")
    r.add_argument("clips", help="JSON con los momentos elegidos")
    r.add_argument("--video", help="override del video fuente")
    r.add_argument("--outdir")
    r.add_argument("--horizontal", action="store_true",
                   help="no reencuadrar a vertical")
    r.add_argument("--words-per-caption", type=int, default=3,
                   help="palabras por bloque de subtítulo (default: 3)")
    r.add_argument("--no-normalize", action="store_true",
                   help="no normalizar el audio")
    r.add_argument("--watermark", help="PNG de marca de agua (ideal con alfa)")
    r.add_argument("--watermark-pos", default="top-right",
                   choices=["top-right", "top-left", "bottom-right", "bottom-left"])
    r.add_argument("--watermark-scale", type=float, default=0.22,
                   help="ancho del logo como fracción del cuadro (default 0.22)")
    r.add_argument("--no-watermark-shadow", action="store_true",
                   help="sin sombra bajo el logo (logos oscuros no la necesitan)")
    r.set_defaults(func=cmd_render)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
