#!/usr/bin/env python3
"""
clipper — de video largo a clips verticales con subtítulos quemados.

Diseño en dos fases, a propósito:

  1. `analyze`  → trabajo de máquina: transcribe con marcas de tiempo.
  2. (criterio) → un humano o un agente lee la transcripción y elige los momentos.
  3. `render`   → trabajo de máquina: corta, reencuadra y quema subtítulos.

La fase 2 NO se automatiza con heurísticas de silencio. Elegir qué momento vale
la pena es criterio, y el criterio se delega a quien tiene contexto.

Requisitos: ffmpeg (con libass), whisper.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

WHISPER = os.environ.get("CLIPPER_WHISPER", "whisper")
FFMPEG = os.environ.get("CLIPPER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("CLIPPER_FFPROBE", "ffprobe")


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


def hhmmss(seconds: float) -> str:
    s = max(0.0, seconds)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ---------------------------------------------------------------- analyze

@dataclass
class Segment:
    idx: int
    start: float
    end: float
    text: str


def transcribe(video: Path, workdir: Path, model: str, lang: str) -> list[Segment]:
    """Extrae audio y transcribe con marcas de tiempo por segmento."""
    wav = workdir / "audio.wav"
    print(f"  extrayendo audio…", flush=True)
    rc = run([FFMPEG, "-y", "-i", str(video), "-ar", "16000", "-ac", "1",
              "-c:a", "pcm_s16le", str(wav)])
    if rc.returncode != 0 or not wav.exists():
        die("ffmpeg no pudo extraer el audio")

    print(f"  transcribiendo (modelo {model})… esto tarda", flush=True)
    rc = run([WHISPER, str(wav), "--model", model, "--language", lang,
              "--task", "transcribe", "--output_format", "json",
              "--output_dir", str(workdir), "--fp16", "False"])
    js = workdir / "audio.json"
    if not js.exists():
        die("whisper no produjo transcripción")

    data = json.loads(js.read_text(encoding="utf-8"))
    segs = []
    for i, s in enumerate(data.get("segments", []), start=1):
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        segs.append(Segment(i, float(s["start"]), float(s["end"]), txt))
    return segs


def cmd_analyze(args) -> int:
    need(FFMPEG); need(FFPROBE); need(WHISPER)
    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        die(f"no existe {video}")

    out = Path(args.out).expanduser().resolve() if args.out else \
        video.parent / f"{video.stem}.transcript.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    total = duration_of(video)
    print(f"analizando {video.name}  ({total/60:.1f} min)")

    with tempfile.TemporaryDirectory(prefix="clipper-") as td:
        segs = transcribe(video, Path(td), args.model, args.lang)

    if not segs:
        die("la transcripción salió vacía")

    payload = {
        "source": str(video),
        "duration_sec": round(total, 2),
        "model": args.model,
        "language": args.lang,
        "segment_count": len(segs),
        "segments": [asdict(s) for s in segs],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Vista legible para que un humano o un agente elija momentos
    readable = out.with_suffix(".txt")
    lines = [f"# {video.name} · {total/60:.1f} min · {len(segs)} segmentos", ""]
    for s in segs:
        lines.append(f"[{s.start:7.1f} → {s.end:7.1f}]  {s.text}")
    readable.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nlisto:")
    print(f"  {out}")
    print(f"  {readable}   ← pásame este archivo para que elija los momentos")
    print(f"\n{len(segs)} segmentos, {total/60:.1f} minutos de material.")
    return 0


# ---------------------------------------------------------------- render

def srt_for_window(segs: list[dict], start: float, end: float) -> str:
    """Genera SRT con tiempos relativos al inicio del clip."""
    out, n = [], 0
    for s in segs:
        s0, s1 = float(s["start"]), float(s["end"])
        if s1 <= start or s0 >= end:
            continue
        a = max(s0, start) - start
        b = min(s1, end) - start
        if b - a < 0.05:
            continue
        n += 1
        out.append(f"{n}\n{hhmmss(a)} --> {hhmmss(b)}\n{s['text'].strip()}\n")
    return "\n".join(out)


VSTYLE = (
    "FontName=DejaVu Sans,FontSize=17,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H90000000,"
    "BorderStyle=3,Outline=2,Shadow=0,Alignment=2,MarginV=60"
)


def render_clip(video: Path, segs: list[dict], clip: dict, outdir: Path,
                vertical: bool, idx: int) -> Path | None:
    start = float(clip["start"])
    end = float(clip["end"])
    if end <= start:
        print(f"  clip {idx}: rango inválido, lo salto")
        return None

    slug = (clip.get("slug") or f"clip{idx:02d}").strip().replace(" ", "-")[:48]
    dur = end - start
    out = outdir / f"{idx:02d}-{slug}.mp4"

    with tempfile.TemporaryDirectory(prefix="clipper-r-") as td:
        td = Path(td)
        srt = td / "s.srt"
        body = srt_for_window(segs, start, end)
        has_subs = bool(body.strip())
        if has_subs:
            srt.write_text(body, encoding="utf-8")

        # Reencuadre vertical 1080x1920 con fondo difuminado del propio video
        if vertical:
            vf = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,gblur=sigma=22[bgb];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
                "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
            )
            if has_subs:
                vf += f";[v]subtitles={srt.name}:force_style='{VSTYLE}'[vo]"
                maps = ["-map", "[vo]"]
            else:
                maps = ["-map", "[v]"]
            cmd = [FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                   "-i", str(video), "-filter_complex", vf, *maps,
                   "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
                   "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac",
                   "-b:a", "128k", "-movflags", "+faststart", str(out)]
        else:
            vf = f"subtitles={srt.name}:force_style='{VSTYLE}'" if has_subs else "null"
            cmd = [FFMPEG, "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                   "-i", str(video), "-vf", vf,
                   "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(out)]

        rc = subprocess.run(cmd, cwd=td, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, check=False)

    if rc.returncode != 0 or not out.exists():
        tail = (rc.stderr or "").strip().splitlines()[-3:]
        print(f"  clip {idx}: FALLÓ")
        for l in tail:
            print(f"      {l}")
        return None

    print(f"  clip {idx}: {out.name}  ({dur:.1f}s, {out.stat().st_size/1e6:.1f} MB)")
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

    print(f"renderizando {len(clips)} clip(s) de {video.name}")
    print(f"formato: {'vertical 1080x1920' if not args.horizontal else 'original'}\n")

    made = []
    for i, c in enumerate(clips, start=1):
        r = render_clip(video, segs, c, outdir, not args.horizontal, i)
        if r:
            made.append(r)

    print(f"\n{len(made)}/{len(clips)} listos en:\n  {outdir}")
    return 0 if made else 1


# ---------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="clipper",
        description="Video largo → clips verticales con subtítulos quemados.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="transcribe con marcas de tiempo")
    a.add_argument("video")
    a.add_argument("--model", default="base",
                   help="tiny|base|small|medium (default: base)")
    a.add_argument("--lang", default="Spanish")
    a.add_argument("--out", help="ruta del .transcript.json")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("render", help="corta y quema subtítulos")
    r.add_argument("transcript", help="el .transcript.json de analyze")
    r.add_argument("clips", help="JSON con los momentos elegidos")
    r.add_argument("--video", help="override del video fuente")
    r.add_argument("--outdir")
    r.add_argument("--horizontal", action="store_true",
                   help="no reencuadrar a vertical")
    r.set_defaults(func=cmd_render)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
