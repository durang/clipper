# clipper

De video largo a clips verticales con subtítulos quemados.

Sin suscripciones, sin subir tu material a un tercero, sin API keys. Corre sobre
`ffmpeg` + `whisper` en tu propia máquina.

---

## Por qué existe

Las herramientas comerciales (Opus Clip, Vizard, Klap) hacen esto bien pero
cobran mensualidad y deciden ellas qué momento vale la pena, con heurísticas
genéricas.

Las skills disponibles hacen la parte mecánica — cortar, transcodificar,
subtitular — pero **ninguna hace la parte difícil: decidir qué momento
importa.** Eso es criterio, no `ffmpeg`.

`clipper` separa las dos cosas a propósito.

---

## El diseño: dos fases y una decisión en medio

```
  1. analyze          2. criterio              3. render
  ─────────           ───────────              ────────
  video ──► whisper   transcripción ──► tú     momentos ──► ffmpeg
            (máquina)  o un agente leen        (máquina)
                       y eligen momentos
                                               clips verticales
                                               con subtítulos
```

**La fase 2 no se automatiza.** No hay detección de silencios ni "picos de
energía". Un humano (o un agente con contexto) lee la transcripción con marcas
de tiempo y decide. Es la única parte que requiere juicio, y es la que hace la
diferencia entre un clip que funciona y uno que no.

---

## Instalación

Requisitos:

- `ffmpeg` compilado con **libass** (para quemar subtítulos)
- `whisper` de OpenAI (`pip install -U openai-whisper`)
- Python 3.9+

```bash
git clone https://github.com/durang/clipper.git
cd clipper
chmod +x clipper.py

# verificar que ffmpeg trae libass
ffmpeg -version | grep libass
```

No hay dependencias de Python más allá de la librería estándar.

---

## Uso

### Fase 1 — analizar

```bash
python3 clipper.py analyze grabacion.mp4 --model base
```

Produce dos archivos:

| Archivo | Para qué |
|---|---|
| `grabacion.transcript.json` | Lo consume `render`. No lo edites. |
| `grabacion.transcript.txt` | **Legible.** Éste es el que le pasas a quien va a elegir los momentos. |

El `.txt` se ve así:

```
# grabacion.mp4 · 42.3 min · 387 segmentos

[   12.4 →    18.9]  La parte difícil no es el código.
[   18.9 →    25.1]  Es decidir dónde vive tu aplicación.
```

**Modelos:** `tiny` (rápido, tosco) · `base` (recomendado) · `small` (mejor,
~3x más lento) · `medium`. En una máquina de 2 CPU, `base` corre cerca de
tiempo real.

### Fase 2 — elegir los momentos

Creas un JSON con los cortes. Le pasas el `.txt` a un agente y le pides que lo
devuelva, o lo escribes a mano:

```json
{
  "clips": [
    {
      "slug": "donde-vive-tu-app",
      "start": 12.4,
      "end": 31.0,
      "why": "Gancho + dolor + remate. Arranca con la tesis y cierra nombrando el hueco."
    },
    {
      "slug": "el-error-de-los-20-minutos",
      "start": 148.2,
      "end": 176.5,
      "why": "Historia concreta con número. Lo más clipeable de la sesión."
    }
  ]
}
```

| Campo | Requerido | Nota |
|---|---|---|
| `start`, `end` | sí | Segundos, decimales permitidos |
| `slug` | no | Va en el nombre del archivo. Default `clipNN` |
| `why` | no | Solo documentación; el programa lo ignora |

### Fase 3 — renderizar

```bash
python3 clipper.py render grabacion.transcript.json clips.json
```

Salida en `grabacion-clips/`:

```
01-donde-vive-tu-app.mp4
02-el-error-de-los-20-minutos.mp4
```

Cada clip: **1080x1920**, H.264, AAC, `faststart`, con subtítulos quemados y
tiempos recalculados al inicio del clip.

Para conservar el encuadre original:

```bash
python3 clipper.py render ... --horizontal
```

---

## Cómo reencuadra a vertical

Un video horizontal metido a 9:16 deja franjas negras. `clipper` hace lo que
usan los editores: duplica el video, difumina una copia como fondo y centra la
otra encima.

```
[0:v]split=2[bg][fg];
[bg]scale=1080:1920:force_original_aspect_ratio=increase,
    crop=1080:1920,gblur=sigma=22[bgb];
[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];
[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]
```

Llena el cuadro sin recortar cabezas y sin barras negras.

---

## Estilo de subtítulos

Pensado para móvil: blanco, negritas, caja semitransparente, centrado abajo.

```
FontName=DejaVu Sans, FontSize=17, Bold=1
PrimaryColour=blanco, BackColour=negro 56%
BorderStyle=3 (caja), Alignment=2 (abajo centro), MarginV=60
```

Se ajusta en la constante `VSTYLE` dentro de `clipper.py`.

---

## Decisiones de diseño

**Por qué no detección automática de momentos.** Las heurísticas de silencio y
energía encuentran dónde alguien *habló fuerte*, no dónde *dijo algo que
importa*. Producen clips promedio. El criterio se delega.

**Por qué SRT y no ASS.** `force_style` sobre SRT cubre el 95% de los casos y se
lee de un vistazo. Si necesitas karaoke o posiciones por palabra, el filtro
`ass` ya está disponible en ffmpeg.

**Por qué `-ss` antes de `-i`.** Búsqueda rápida por keyframe: recorta antes de
decodificar. En archivos de una hora es la diferencia entre segundos y minutos.

**Por qué re-encoda en lugar de copiar streams.** Copiar exige cortar en
keyframe, lo que desplaza el inicio hasta segundos. Re-encodar da el corte
exacto que pediste.

**Por qué falla en vez de adivinar.** Si la transcripción sale vacía, si el
rango es inválido o si ffmpeg revienta, el programa lo dice y no produce un
archivo a medias. Un clip parcial publicado es peor que ningún clip.

---

## Verificado

Probado de punta a punta en Amazon Linux 2023, 2 vCPU:

- Video de 27.8s con voz en español → 6 segmentos transcritos, tiempos exactos
- 1 clip renderizado: **1080x1920**, 15.4s, subtítulos quemados, 0.3 MB

---

## Limitaciones conocidas

- Whisper en CPU es lento. Una grabación de una hora con `base` toma un rato;
  lánzalo en segundo plano.
- Modelos `tiny` y `base` cometen errores con nombres propios y tecnicismos.
  Para material que se publica, revisa el `.txt` antes de renderizar.
- El desenfoque de fondo agrega costo de CPU. Con muchos clips, considera
  `--horizontal` y reencuadrar después.
- Sin detección de escena ni de hablante.

---

## Licencia

MIT
