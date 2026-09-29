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

## El diseño: tres fases mecánicas y una decisión en medio

```
  0. fetch        1. analyze          2. criterio           3. render
  ────────        ─────────           ───────────           ────────
  URL ──► yt-dlp  video ──► whisper   transcripción ──► tú  momentos ──► ffmpeg
  (opcional)      (máquina)           o un agente leen      (máquina)
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
- `yt-dlp` — opcional, solo para `fetch` (`pip install -U yt-dlp`)
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

### Fase 0 — bajar (opcional)

Si el material está en YouTube, Reels, TikTok, X o cualquiera de los ~1800 sitios
que soporta yt-dlp:

```bash
python3 clipper.py fetch "https://youtube.com/watch?v=..."

# bajar y transcribir de un tirón
python3 clipper.py fetch "https://..." --analyze --model base
```

Prefiere H.264 + AAC a ≤1080p, que es lo que ffmpeg recorta sin sorpresas.

**YouTube desde un servidor:** YouTube bloquea IPs de centro de datos con
*«Sign in to confirm you're not a bot»*. Verificado: falla desde EC2. Soluciones:

```bash
# desde tu propia máquina, con el navegador abierto
python3 clipper.py fetch "https://..." --cookies-from-browser chrome

# o con cookies exportadas
python3 clipper.py fetch "https://..." --cookies cookies.txt
```

URLs directas a `.mp4` y la mayoría de los otros sitios funcionan sin cookies.

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

**Idioma:** por defecto `--lang auto`: detecta con 30 s de audio (desde el 10 % del video, para
esquivar intros y música) y el modelo `tiny`. Si pasas `--lang es` **se verifica** contra el audio:
si suena a otro idioma, se detiene (código 3) en vez de transcribir inglés como español — error real
que costó 50 minutos. `--force-lang` salta la verificación. Si no puede detectar, falla; nunca asume.

**Diccionario permanente:** antes de escribir la transcripción corrige los nombres que Whisper
escribe como suenan (ver [Diccionario](#diccionario-permanente)).

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

Cada clip: **1080x1920**, H.264, AAC, `faststart`, subtítulos quemados
palabra por palabra, audio normalizado y tiempos recalculados al inicio del clip.

Opciones:

```bash
--horizontal              # conservar el encuadre original
--fit crop --crop-x 0.4   # vertical por recorte 9:16 (sin fondo difuminado), centrado en x
--tighten 0.35            # quita silencios > 0.35 s, nunca dentro de una palabra
--cover-subs 0.2          # difumina el 20 % inferior del original (subtítulos quemados)
--words-per-caption 2     # palabras por bloque (default 3; además, nunca más de lo que cabe)
--no-highlight            # sin resaltar la palabra que se está diciendo
--no-captions             # solo cortar: para montar después con /edit-video
--cliente acme            # aplica también el diccionario privado de ese cliente
--no-normalize            # no tocar el audio
```

Todo lo de encuadre se puede fijar **por clip** en el JSON (`fit`, `crop_x`, `tighten`,
`cover_subs`): el agente mira los frames de cada momento y decide dónde está la cara.

### El campo `hook`

Si un clip trae `hook`, ese texto aparece **grande, en amarillo, arriba, los
primeros 3 segundos**. Es lo que decide si alguien se queda:

```json
{ "slug": "donde-vive", "start": 12.4, "end": 31.0,
  "hook": "NADIE TE DICE ESTO" }
```

### Aviso de duración por plataforma

Al renderizar, avisa si el clip excede el límite de cada red:

| Plataforma | Límite |
|---|---|
| X / Twitter | 140 s |
| Instagram Reels | 90 s |
| YouTube Shorts | 180 s |
| TikTok | 600 s |

---

## Cómo reencuadra a vertical

Dos modos:

- **`--fit crop`** (el mejor cuando hay una persona): recorte 9:16 a pantalla completa, centrado en
  `crop_x` (0 = izquierda, 1 = derecha). Sin franjas ni fondo borroso. Si el video cambia de plano y
  la cara se mueve, se decide por clip.
- **`--fit blur`** (default, seguro): duplica el video, difumina una copia como fondo y centra la
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

## Subtítulos palabra por palabra

Whisper entrega tiempos **por palabra** (`--word_timestamps`). `clipper` los
agrupa en bloques de 1–3 palabras que se suceden rápido — el estilo que domina
en Reels, TikTok y Shorts, y el que mide mejor retención que el subtítulo largo.

- **La palabra que se está diciendo se resalta** en amarillo (el color del gancho). Se desactiva con
  `--no-highlight`.
- **Cada bloque cabe en una línea**: además del número de palabras, se limita por caracteres según el
  tamaño de letra. Un bloque como "Colombia, first nearshoring," se salía por los dos lados.
- **Zona segura**: en vertical el subtítulo queda por encima del 20 % inferior (y < 1536 en 1920),
  que Reels, TikTok y Shorts tapan con el texto del post y los botones.

Se emiten como **ASS** (no SRT) para tener control real de tamaño, contorno y
posición a 1080x1920. Blanco, negritas, contorno negro grueso, centrado abajo.

Si la transcripción no trae palabras, cae automáticamente a subtítulo por
segmento. Nunca falla por eso.

**La fuente se detecta en tiempo de ejecución** con `fc-match`, probando Noto
Sans, DejaVu Sans, Liberation Sans y Arial en ese orden. Codificar una fuente
fija es un error común: si no existe en el sistema, libass sustituye por
cualquiera y el resultado se ve mal sin avisar.

## Normalización de audio

Cada clip pasa por `loudnorm=I=-16:TP=-1.5:LRA=11` (EBU R128, el objetivo
estándar de redes). Sin esto, unos clips salen susurrando y otros gritando.
Se desactiva con `--no-normalize`.

---

## Decisiones de diseño

**Por qué no detección automática de momentos.** Las heurísticas de silencio y
energía encuentran dónde alguien *habló fuerte*, no dónde *dijo algo que
importa*. Producen clips promedio. El criterio se delega.

**Por qué ASS y no SRT.** SRT con `force_style` no permite resaltar una palabra dentro del bloque
ni fijar posición y contorno con precisión a 1080x1920. ASS sí, y libass lo quema igual.

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

- `fetch` con URL directa → archivo bajado, duración y tamaño detectados
- `fetch` con YouTube desde EC2 → **falla con bloqueo de bot**, y el programa
  imprime la instrucción de cookies en lugar de morir con un stacktrace
- Video con voz en español → 3 segmentos, **34 palabras con tiempo individual**
- Fuente detectada en tiempo de ejecución: **Noto Sans** (DejaVu no existe en
  Amazon Linux 2023 — bug real encontrado y corregido)
- Clip renderizado: **1080x1920**, h264 + aac, subtítulos palabra por palabra,
  gancho de 3s, audio normalizado
- `drawtext` **no** está compilado en el ffmpeg probado; el gancho se resuelve
  con ASS, que sí funciona vía libass

Y sobre una entrevista real de feria (inglés, subtítulos quemados, 2 vCPU):

- Diccionario: "Columbia" → Colombia, "near Turing" → nearshoring (dos palabras fundidas en una con
  sus tiempos), "EmoQs" → MOQs; "sol" no toca "girasol"; aplicarlo dos veces no cambia nada
- Bloque "Colombia, first nearshoring," se salía del cuadro → ahora se parte para que quepa
- Palabra activa en amarillo, subtítulo por encima de y = 1536, franja inferior del original difuminada
- `--tighten`: audio y video salen con la misma duración (13.07 / 13.10 s) y los subtítulos siguen a la voz
- 3 clips (40 s de salida) en vertical, preset veryfast: **69 s** de render en 2 vCPU

---

## Diccionario permanente

Whisper escribe los nombres como suenan. Una vez corregido, un nombre no se vuelve a corregir:

```bash
python3 clipper.py dict agregar "Columbia" "Colombia"
python3 clipper.py dict agregar "Acme Corp" "ACME" --cliente acme     # privado de ese cliente
python3 clipper.py dict ver
```

Se aplica en `analyze` (la transcripción ya sale bien) y otra vez en `render` (vale lo que agregaste
después). Reglas: **palabra completa** ("sol" no toca "girasol"), sin mayúsculas, y las correcciones
de **varias palabras** ("near Turing" → "nearshoring") funden los tiempos por palabra, para que el
subtítulo palabra por palabra también salga bien.

Capas, la última gana:

| Capa | Archivo |
|---|---|
| Studio / global | `~/clipper-studio/dictionary.json` (o `CLIPPER_DICT`) |
| Compartido con `/edit-video` | `~/.config/edit-video/diccionario.json` |
| Cliente (privado) | `<área de clientes>/clients/<slug>/diccionario.json` |
| Proyecto | `diccionario.json` junto al video |

## Silencios fuera

```bash
python3 clipper.py render t.json clips.json --tighten 0.35          # en el render
python3 clipper.py tighten t.json --start 12.4 --end 31 > keep.json  # solo los tramos (JSON)
```

Corta solo **entre** palabras, deja 0.12 s de aire a cada lado, y recalcula los subtítulos al nuevo
tiempo. `tighten` imprime los tramos a conservar: es lo que usa `/edit-video` para su rough cut.
Ojo: necesita tiempos por palabra con huecos reales (Whisper de OpenAI los da; whisper.cpp tiende a
pegar el final de una palabra con el inicio de la siguiente).

## Con /edit-video

`clipper` es el **volumen**; [`/edit-video`](https://github.com/durang/edit-video) es el **nivel
estudio** (motion design sobre HyperFrames). Lo que más rinde: clipper saca todos los clips rápidos y
los 1–3 mejores se cortan con `--no-captions` y se montan en `/edit-video`. Comparten diccionario y
área de clientes.

## Caché de transcripción

`analyze` guarda una huella del video (tamaño + primer y último MB). Si vuelves
a correrlo sobre el mismo archivo, reusa la transcripción en lugar de repetir
whisper — que en CPU es la parte lenta. Con `--force` la rehace.

## Mejoras pendientes

| Mejora | Por qué sirve | Esfuerzo |
|---|---|---|
| **Corte por escena** | `ffmpeg` detecta cambios de escena; alinear los cortes ahí evita empezar a media palabra visual. | medio |
| **Modo lote** | Una carpeta de grabaciones → analizar todas de un tirón. | bajo |
| **Exportar miniaturas** | Frame representativo por clip, listo para portada. | bajo |
| **Recorte por cara automático** | Detectar la cara en los frames y fijar `crop_x` solo. Hoy lo decide el agente mirando los frames. | medio |
| **Recorte por hablante activo** | Con dos personas en cuadro, seguir a quien habla. | alto |
| **Silencios por energía** | Complementar `--tighten` con `silencedetect` cuando los tiempos por palabra vienen pegados. | bajo |

---

## Limitaciones conocidas

- Whisper en CPU es lento. Una grabación de una hora con `base` toma un rato;
  lánzalo en segundo plano.
- YouTube bloquea descargas desde IPs de centro de datos; requiere cookies.
- Modelos `tiny` y `base` cometen errores con nombres propios y tecnicismos.
  Para material que se publica, revisa el `.txt` antes de renderizar.
- El desenfoque de fondo agrega costo de CPU. Con muchos clips, considera
  `--horizontal` y reencuadrar después.
- Sin detección de escena ni de hablante. `--fit crop` usa un solo `crop_x` por clip: si el clip
  cambia de plano y la cara se mueve mucho, usa `--fit blur` o parte el clip.

---

## Licencia

MIT

## Clipper Studio (interfaz web)

Interfaz de navegador para el flujo completo, sin tocar la terminal.

    python3 studio.py          # escucha en 127.0.0.1:8791

Variables: STUDIO_PORT, STUDIO_DIR, CLIPPER, WHISPER_BIN, STUDIO_MAX_BYTES.

Pasos en pantalla:

1. Subir video (arrastrar y soltar, hasta 4 GB)
2. Transcribir — idioma manual o **deteccion automatica** (recorta 30 s y usa el
   modelo tiny antes de la transcripcion completa)
3. Corregir subtitulos — edicion por segmento, buscar-y-reemplazar y
   **diccionario permanente** que se aplica solo en todos los videos futuros
4. Marcar momentos — botones I/F sobre cada linea de la transcripcion
5. Salida — vertical / horizontal / YouTube a la vez, logo, escala de marca de
   agua, palabras por subtitulo y CRF
6. Descargar cada clip o el ZIP completo

Solo libreria estandar de Python. Escucha unicamente en loopback; la exposicion
se hace por Tailscale serve. Toda ruta de descarga se valida contra el
directorio del trabajo.
