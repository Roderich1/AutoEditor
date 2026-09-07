# Content Engine

CLI local para convertir videos largos en clips publicables. Esta primera entrega
implementa la base reproducible del pipeline: diagnóstico, inspección de medios,
creación de ejecuciones, extracción de audio y transcripción.

## Requisitos

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- FFmpeg y FFprobe disponibles en `PATH`
- Una compilación de FFmpeg con el filtro `ass`

## Inicio rápido

```console
uv sync --extra transcription
uv run content-engine doctor
uv run content-engine inspect sample.mp4
uv run content-engine run sample.mp4 --config configs/fast.toml
uv run content-engine transcribe RUN_ID --config configs/fast.toml
uv run content-engine analyze RUN_ID                      # llama a Gemini
uv run content-engine analyze RUN_ID --fixture fixture.json   # reproduce un archivo
uv run content-engine preview RUN_ID                      # proxies 540x960
uv run content-engine review RUN_ID                       # decisión humana
uv run content-engine render RUN_ID                       # clips 1080x1920
```

`doctor --require-ai` convierte las credenciales y el modelo de análisis en
requisitos obligatorios; por defecto son advertencias.

`analyze` tiene dos modos y el manifiesto siempre registra cuál se usó.

Con `--fixture` se reproducen respuestas grabadas en un archivo: sin red, sin
credencial y sin consultar `GEMINI_API_KEY`. El analizador se identifica como
`fixture`, de modo que ninguna ejecución puede afirmar una llamada que nunca
ocurrió.

Sin `--fixture` decide `analysis.provider`. Para `gemini` eso significa una
llamada real por chunk contra `GEMINI_API_KEY`, con el prompt versionado
`clip_candidates/v1` y salida estructurada. La clave se lee del entorno
únicamente cuando se construye el proveedor real, nunca llega a un artefacto ni
a un mensaje, y su ausencia es un error de configuración (código 2) que deja el
run intacto.

Los artefactos producidos por un modo nunca se reutilizan en el otro: la
configuración efectiva de la etapa nombra al analizador que realmente corrió y
su digest es lo que decide la reutilización.

Reutilizar no requiere credencial. La identidad que un run *tendría* se calcula
sin SDK, sin cliente y sin leer el entorno, así que verificar cuatro artefactos
ya terminados funciona en una máquina que ya no tiene la clave. Solo producir
—primera ejecución o `--force`— valida el SDK, el modelo y la credencial.

`analysis.prompt_version` selecciona el prompt: `v1` resuelve al recurso
empaquetado y a la identidad `clip_candidates/v1` que queda registrada. Una
versión desconocida se rechaza con código 2 antes de tocar el run, también en
modo fixture. El manifiesto registra `prompt_version` y `prompt_sha256` reales
en una ejecución con proveedor y `null` en una con fixture.

Un fallo real del proveedor deja el run en `FAILED_ANALYSIS` (código 5) y no
escribe ningún artefacto. Los reintentos se limitan a fallos transitorios
—timeout, 429, 5xx— con tres intentos y espera acotada; un 400, 401 o 403 no se
reintenta (ADR-027).

## Evaluación humana

Ni `preview` ni `review` leen `GEMINI_API_KEY`, ni abren un socket, ni vuelven a
llamar al proveedor: trabajan sobre candidatos ya generados.

### `preview` — proxies de bajo costo (CE-034)

```console
uv run content-engine preview RUN_ID [--config PATH] [--force]
```

Un proxy por candidato seleccionado, en `previews/candidate_<id>.mp4`: 540x960,
H.264 y AAC, `veryfast` con CRF 30. El encuadre completo del origen se ajusta
dentro del marco vertical y se rellena con negro, con `setsar=1`, así que la
imagen no se estira ni se recorta —en una grabación técnica lo importante suele
estar en una esquina de la terminal—. Sin subtítulos y sin estilo final: eso es
CE-040 a CE-045.

FFmpeg se invoca con una lista de argumentos, nunca con `shell=True`. Cada
codificación ocurre en `previews/.staging/` y se verifica allí con ffprobe
—dimensiones, códecs y duración contra una tolerancia documentada de 1,0 s—;
nada se mueve a `previews/` hasta que el conjunto completo pasa.

La publicación es **duradera, no atómica**, y la diferencia importa (ADR-031).
El conjunto publicado se aparta a `previews/.rollback/` antes de colocar el
nuevo, así que un fallo al publicar restaura el anterior byte a byte. Si falla la
restauración misma —un disco lleno, un permiso revocado—, ninguna operación
puede completarse por decreto: lo que se garantiza es que **no se pierde nada**.
Cada archivo del conjunto anterior queda en `previews/` o en
`previews/.rollback/`, el respaldo no se borra mientras la restauración esté
incompleta, el error nombra el directorio que contiene los datos, y la siguiente
ejecución de `preview` termina la restauración.

Terminarla es seguro desde cualquier punto porque `previews/.rollback/rollback.json`
registra la fase, y son tres:

| Fase | Estado de `previews/` | Qué puede hacer el deshacer |
|---|---|---|
| `moving_aside` | parte del conjunto anterior sigue aquí; nada nuevo colocado | solo mover de vuelta — **no borra nada** |
| `placing` | todo lo anterior está en el respaldo; lo que hay aquí es nuevo | borrar eso y pasar a `restoring` |
| `restoring` | el borrado terminó; lo que hay aquí ya está recuperado | solo mover de vuelta — **no borra nada** |

`restoring` se escribe **después del último borrado y antes del primer archivo
devuelto**. Sin esa tercera fase, una restauración interrumpida a mitad se
reanudaba como si el directorio siguiera lleno de archivos nuevos y borraba
justamente los que ya había recuperado. Mover un archivo de vuelta es
idempotente, así que reanudar continúa con lo que quede; y si falla la escritura
de la fase, nada se ha movido todavía y una ejecución posterior repite sin
riesgo. Un respaldo cuyo diario falte o no se pueda interpretar se rechaza sin
tocarlo, porque la fase decide qué archivos se eliminan y adivinarla borraría los
que no tienen otra copia.

`previews/index.json` describe cada archivo con su intervalo, su duración
esperada y medida, sus dimensiones, sus códecs, su SHA-256 y su tamaño, junto al
fingerprint del análisis y al digest del origen del que se cortó. Un preview
borrado, truncado, editado o renombrado impide la reutilización, igual que un
cambio de dimensiones, de reglas, de análisis o de origen.

Si `preview.enabled = false`, el comando se rechaza con código 2 en lugar de
avanzar: dejar un run en `READY_FOR_REVIEW` con un directorio vacío sería
afirmar previews que nunca se generaron. Si el análisis no seleccionó ningún
candidato, el comando avisa, escribe un índice vacío y honesto, y el run avanza
igualmente.

### `review` — decisión editorial (CE-035 a CE-039)

```console
uv run content-engine review RUN_ID [--config PATH] [--force]
```

Un candidato a la vez, con su posición, rank, ID, topic, categoría, inicio, fin,
duración, total, las seis puntuaciones componentes, hook, resumen, motivo y la
ruta de su preview. Después, cinco teclas:

```text
[A] Approve      aprueba el intervalo propuesto
[R] Reject       rechaza, con motivo estructurado opcional
[E] Edit range   corrige inicio y fin
[S] Skip         no decide nada; el candidato vuelve a aparecer
[Q] Quit/save    termina conservando lo ya decidido
```

Cada decisión explícita se guarda de forma atómica **antes** de mostrar el
siguiente candidato, así que una terminal cerrada no cuesta nada de lo ya
decidido. `S` no crea una decisión falsa: ausencia significa «todavía sin
decidir», que es lo que permite reanudar exactamente lo pendiente. `Q`, EOF y
Ctrl+C terminan la sesión sin marcar el run como fallido. El run llega a
`REVIEWED` solo cuando cada candidato seleccionado tiene una decisión explícita.

`review/decisions.json` está versionado y es estricto. Hay tres tipos de
decisión discriminados por `decision`, no un modelo con campos opcionales
(ADR-029): una aprobación conserva el intervalo original, una edición debe
diferir realmente de él, y un rechazo **no tiene límites finales** —no se aprobó
ningún intervalo, y fingirlos produciría una fila que se lee como un clip
aprobado—. El motivo de rechazo usa un enum propio, `EditorialReason`, separado
del `RejectionReason` del pipeline: uno describe por qué el sistema descartó una
propuesta, el otro un juicio editorial sobre material que pasó todas las reglas.
`other` es el único motivo que exige detalle libre, porque por sí solo no dice
nada.

Una edición humana no está limitada por `min_duration_seconds` ni
`max_duration_seconds`: esa política acota lo que el modelo puede proponer, y
quien mira el preview es la autoridad sobre dónde termina su clip. Solo se exige
que sea finita, ordenada y dentro del origen.

`--force` advierte cuántas decisiones va a descartar antes de preguntar nada, y
no escribe hasta la primera decisión nueva, de modo que una sesión forzada y
abandonada de inmediato conserva las anteriores. Sobre un run `REVIEWED` devuelve
el estado a `READY_FOR_REVIEW`: es la única transición hacia atrás de la máquina
de estados, y existe porque un run cuyas decisiones acaban de borrarse no puede
seguir afirmando una revisión terminada (ADR-030).

## Render final

`render` no lee `GEMINI_API_KEY`, no abre un socket y no vuelve a llamar al
proveedor. Trabaja sobre artefactos que ya existen.

### `render` — clips verticales definitivos (CE-040 a CE-046)

```console
uv run content-engine render RUN_ID [--config PATH] [--force]
```

Un directorio por decisión conservada, en `clips/clip_<candidate_id>/`:

```text
clips/
├── clip_cand_3762331fe118880c/
│   ├── clip.mp4          # 1080x1920, H.264, AAC, píxeles cuadrados
│   ├── subtitles.srt
│   ├── subtitles.ass
│   └── metadata.json
├── index.json
└── config.effective.json
```

Exige un run `REVIEWED` y permite el reintento controlado desde
`FAILED_RENDER`. Una revisión incompleta se rechaza: un candidato sin decisión
no es uno que alguien haya descartado, sino uno al que todavía no ha llegado.

**Qué se renderiza.** Una aprobación se corta sobre el intervalo del propio
candidato. Una edición se corta sobre `final_start` y `final_end` y nada más: no
se ensancha hasta el mínimo del analizador ni se vuelve a ajustar a un borde de
segmento, porque quien miró el preview es la autoridad sobre dónde termina su
clip. Un rechazo no produce nada. El orden es el rango del candidato, nunca el
orden en que se respondieron las decisiones, así que revisar la lista al revés o
retomar una sesión a la mitad da los mismos clips en el mismo orden. Los rangos
conservan los huecos que deja un rechazo: el rango 4 junto al 1 es el registro
honesto.

Si la revisión rechazó todo, la etapa termina con éxito, escribe un índice vacío
y avisa. Rechazarlo todo es un resultado, no un fallo, y fabricar un clip sería
inventar una decisión que nadie tomó (ADR-034).

### Subtítulos

Se construyen desde los timestamps de palabra del transcript, nunca desde los
límites de segmento. Un transcript sin timestamps de palabra se rechaza nombrando
`transcription.word_timestamps`: aproximarlos daría bloques de cinco a treinta
segundos que aparecen de golpe, un artefacto que afirma una sincronización que no
tiene (ADR-032).

Un intervalo sin habla es distinto y es legítimo: produce un SRT vacío y un ASS
con solo su cabecera.

```text
local = absolute - final_start
```

Las palabras que cruzan un borde se recortan; una que termina exactamente en el
inicio del clip no comparte tiempo con él y queda fuera. La agrupación es
determinista: hasta 8 palabras, hasta 2 líneas, corte en fin de frase, en una
pausa de 0.6 s o más, en una coma cuando la señal ya lleva 6 palabras, y en el
presupuesto de caracteres de dos líneas.

| Formato | Redondeo | Nota |
|---|---|---|
| SRT | milisegundos, `ROUND_HALF_UP` | numerado desde 1 |
| ASS | centésimas, `ROUND_HALF_UP` | `PlayRes` del propio clip, sin re-ajuste de línea |

`ROUND_HALF_UP` explícito porque `round()` de Python es *half-even* y pondría
0.0005 s en 0 ms. Cada señal recibe una duración mínima visible **antes** de
redondear, así que ninguna puede colapsar a cero ni desordenarse.

`{`, `}` y `\` se transliteran a `(`, `)` y `/` en el ASS. El formato no ofrece
un escape portable para ellos y libass y VSFilter no coinciden sobre una llave
sin cerrar. El SRT y `transcript.json` conservan el texto exacto.

### Presets

| Preset | Qué hace |
|---|---|
| `vertical_blur` | fondo escalado para cubrir 9:16, recortado y desenfocado; primer plano escalado para caber entero y superpuesto centrado |
| `vertical_crop` | escalado para cubrir 9:16 y recorte central |

`vertical_blur` es el predeterminado para grabaciones de pantalla: un recorte
duro se lleva la esquina de la terminal donde suele estar el punto del clip. Nada
usa un `scale=w:h` a secas, que deformaría cualquier origen que no sea ya 9:16.
Ambos escaladores llevan `force_divisible_by=2` porque `yuv420p` no puede
representar una dimensión impar, y ambos terminan en `setsar=1`.

La lista de argumentos de FFmpeg la construye una función pura y se verifica
elemento por elemento. `-ss` va antes de `-i` para que el codificador busque en
lugar de decodificar hasta el intervalo —lo que además rebasa los timestamps de
salida a cero, que es lo que hace que los subtítulos locales cuadren— y `-t` va
después para que el límite se aplique a lo que se escribe. El *texto* del
subtítulo nunca aparece en el comando: está en un archivo, y al filtro se le pasa
la ruta.

**Ninguna ruta entra en el grafo de filtros.** `ass=` recibe su archivo como
valor de opción dentro del grafo, y un valor de opción se desescapa **dos
veces**: una al partir el grafo y otra al leer las opciones del filtro. Una
primera versión escapaba ahí la ruta absoluta y tenía una prueba unitaria que
comparaba la cadena escapada; la cadena era correcta y FFmpeg abría otro archivo:

```text
Could not create a libass track when reading file '/tmp/codex its ñ/subtitles.ass'
```

El apóstrofo desaparece, porque el `'''` que sobrevive correctamente al primer
paso vuelve a leerse como comilla en el segundo. Escapar dos veces arreglaría
este caso y rompería el contrario, y el número de pasos no lo decide este
código.

Así que FFmpeg se ejecuta **en el directorio del propio clip** y al filtro se le
pasa el nombre a secas, `subtitles.ass`, que él mismo resuelve. `source` y
`output` siguen siendo absolutos y van como argumentos, que ningún analizador
toca, de modo que mover el directorio de trabajo no puede cambiar qué archivos
son. La prueba que lo respalda se la hace a FFmpeg, bajo un directorio que de
verdad se llama `codex it's ñ`, e incluye un render A/B que compara la banda del
subtítulo: FFmpeg termina con 0 haya dibujado libass algo o no. ADR-035.

### Verificación

Nada de lo pedido a FFmpeg se da por hecho. Cada clip se lee con ffprobe en el
directorio de staging —dimensiones, relación de aspecto de píxel, ambos códecs y
duración contra una tolerancia documentada de 1.0 s— y ambos subtítulos se
vuelven a parsear y se comprueba su orden y sus límites, antes de publicar nada.

La relación de aspecto de píxel se comprueba porque 1080x1920 con píxeles no
cuadrados no es un video vertical, y ninguna otra comprobación notaría un render
que perdiera `setsar`.

Una invocación posterior lo prueba todo otra vez sin codificador: tamaño y
digest de **los cuatro** artefactos —`metadata.json` incluido—, cada
`metadata.json` contra su registro campo por campo, ambos documentos parseados,
ningún archivo de más dentro de un directorio de clip, ningún directorio de clip
que el índice no nombre, el conjunto contra las decisiones que la revisión
registró, y el fingerprint reconstruido. Un artefacto borrado, truncado,
manipulado o renombrado no se reutiliza, y el rechazo nombra `--force`.

`metadata.json` estuvo fuera de todo digest en una primera versión, con el
argumento de que un archivo no puede contener su propio hash. Era cierto e
irrelevante: nada obliga a que el hash esté *dentro*. `ClipRecord` guarda
`metadata_sha256` y `metadata_size_bytes`, y el orden deshace el ciclo aparente
—medir, construir el metadata, escribirlo, hashear el archivo escrito, y
entonces construir el registro—. La comparación campo por campo se mantiene como
segunda capa: un digest demuestra que los bytes no se movieron, la comparación
demuestra que los dos artefactos siguen diciendo lo mismo. ADR-036.

El fingerprint de la revisión se **reconstruye** desde `decisions.json` en lugar
de leerse del manifiesto. Eso detecta un archivo de decisiones editado después de
registrarse la revisión: el único artefacto del motor que no se puede regenerar, y
el único cuya alteración cambiaría en silencio lo que se publica.

### Publicación

`clips/` se reemplaza con el mismo protocolo duradero que `previews/`, compartido
en `services/publication.py` (ADR-033). La diferencia es que aquí un elemento es
un **directorio** de cuatro artefactos y no un archivo suelto.

La garantía es **durabilidad, no atomicidad**: o se publica el conjunto nuevo, o
se restaura el anterior byte a byte, o —si la restauración misma falla— cada
artefacto sigue en `clips/` o en `clips/.rollback/`, el respaldo no se borra, el
error nombra el directorio que tiene los datos, y la siguiente ejecución de
`render` termina el trabajo desde la fase anotada en el diario.

## Configuración

Los valores predeterminados viven dentro del paquete, en
`content_engine/resources/default.toml`, y se leen con `importlib.resources`.
Esa es la única copia canónica: los modelos Pydantic validan tipos e invariantes
pero no repiten los valores. La configuración funciona igual desde el repositorio,
desde un wheel instalado y desde cualquier directorio de trabajo.

### Perfiles

`configs/fast.toml` y `configs/quality.toml` son overlays versionados que se
fusionan sobre los valores canónicos:

```console
uv run content-engine run sample.mp4 --config configs/quality.toml
```

Son perfiles semánticos, no atajos: `fast` significa "el más rápido que sigue
siendo útil" y `quality` significa "el mejor disponible". `quality.toml` coincide
hoy con los valores por omisión, y eso es intencional — nombra una intención que
sobrevive a un cambio de los defaults. Viven en el repositorio, no dentro del
paquete, así que una instalación solo-wheel no los trae; cualquier TOML externo
sirve igual mediante `--config`. Empaquetar perfiles incorporados o añadir un
`--profile` queda fuera de esta entrega.

Una clave desconocida, una relación inválida o un número no finito (`nan`, `inf`)
se rechazan indicando exactamente qué falla, en lugar de ignorarse en silencio.

Variables de entorno:

| Variable | Efecto |
|---|---|
| `CONTENT_ENGINE_WORKSPACE` | Raíz del workspace; tiene prioridad sobre el TOML |
| `CONTENT_ENGINE_ANALYSIS_MODEL` | Modelo de análisis |
| `GEMINI_API_KEY` | Credencial de análisis (ADR-019). Se lee solo al construir el proveedor real; nunca se escribe en artefactos, manifiesto ni mensajes |
| `CONTENT_ENGINE_RUN_AI_TESTS` | `1` habilita la única prueba que gasta cuota real (`tests/ai/`); por defecto se omite |

Un `workspace.root` relativo se resuelve contra el directorio actual, nunca
contra el directorio de instalación. `doctor` y `run` imprimen siempre la ruta
absoluta resuelta.

## Ejecuciones

Cada ejecución vive en `workspace/runs/RUN_ID` y es un experimento:

- `manifest.json` — estado, hashes, versiones y etapas completadas
- `config.effective.json` — configuración con la que se **creó** el run
- `media/probe.json`, `audio/source.wav`
- `transcript/` — `transcript.json`, `.txt`, `.srt`, `metrics.json` y
  `config.effective.json`
- `analysis/` — `chunks.json`, `candidates.raw.json`, `candidates.json` y
  `config.effective.json`
- `previews/` — `candidate_<id>.mp4`, `index.json` y `config.effective.json`
- `review/` — `decisions.json` y `config.effective.json`
- `clips/` — un `clip_<id>/` por decisión conservada con `clip.mp4`,
  `subtitles.srt`, `subtitles.ass` y `metadata.json`, más `index.json` y
  `config.effective.json`

### Dos niveles de configuración

Un run guarda dos configuraciones, deliberadamente:

| Artefacto | Qué describe |
|---|---|
| `config.effective.json` (raíz) | La configuración con la que se creó el experimento |
| `transcript/config.effective.json` | Lo que la etapa de transcripción ejecutó realmente |
| `analysis/config.effective.json` | Lo que la etapa de análisis ejecutó realmente |
| `previews/config.effective.json` | Lo que la etapa de preview ejecutó realmente |
| `review/config.effective.json` | Sobre qué material se tomaron las decisiones |
| `clips/config.effective.json` | La política con la que se renderizó: preset, códecs, reglas y estilo de subtítulo |

Difieren siempre que `transcribe --config` apunta a otro perfil, algo legítimo
—es como se compara un modelo contra otro sobre el mismo audio— pero nunca
silencioso: el comando avisa de la divergencia. La configuración de etapa además
resuelve `auto` al dispositivo y al tipo de cómputo que la máquina eligió, cosa
que la del run no puede saber.

`manifest.stages.transcription` guarda el `fingerprint`, que decide si un
transcript puede reutilizarse, y `stage_config_sha256`, que ata el manifiesto a
ese artefacto legible. El fingerprint decide; la configuración de etapa explica.
`manifest.versions.transcription_model` nombra el modelo que realmente produjo el
transcript, no el que estaba configurado al crear el run.

El `run_id` identifica una ejecución; `config_sha256` identifica el experimento y
es idéntico entre máquinas. `transcribe` reutiliza un transcript solamente cuando
su fingerprint coincide con el audio y las opciones actuales, incluido el hardware
realmente resuelto; si no coincide, lo rechaza y explica por qué en lugar de
mezclar artefactos incompatibles. `--force` regenera.

`analyze` sigue la misma disciplina con cuatro artefactos en lugar de uno: los
recalcula todos en memoria antes de escribir nada, y para reutilizarlos exige que
los cuatro estén presentes, se lean y validen contra su propio esquema, que
concuerden entre sí y con el transcript actual, que los chunks en disco sean los
que este transcript y esta configuración producen, que el fingerprint se
reconstruya desde los cuatro más el transcript, y que la configuración pedida
ahora sea la registrada. Editar cualquier campo de cualquiera de los cuatro
—un `topic`, un intervalo, una puntuación, el orden del ranking— se rechaza con
código 3 sin tocar nada.

Si el run había quedado en `FAILED_ANALYSIS` por un `--force` fallido y los
artefactos anteriores siguen coincidiendo con todas las entradas, la
verificación demuestra que la etapa está completa: el run vuelve a `ANALYZED` y
el fallo se limpia. Un rechazo no recupera nada.

`analysis/config.effective.json` registra qué analizador ejecutó de verdad junto
al proveedor que la configuración nombraba, y `manifest.versions.analysis_provider`
guarda el ejecutor real. Un run analizado desde un fixture no puede quedar con un
manifiesto que diga `gemini`.

Una ejecución que falla conserva su directorio, su estado `FAILED_*` y el motivo,
para poder diagnosticarla.

Todas las rutas se construyen con `pathlib.Path` y **todos** los artefactos se
escriben de forma atómica, en UTF-8 con saltos LF y sin BOM, por lo que el mismo
código produce los mismos bytes en Windows 11 y Ubuntu 24.04. Un fallo de
escritura no deja ni un archivo parcial ni un `.tmp`. `.gitattributes` aplica la
misma política al texto del propio repositorio.

La excepción a la comparabilidad byte a byte son los `.mp4`, de preview y de
render: x264 incrusta su propia identidad de compilación y su salida no es
determinista entre versiones. En un clip se suma que libass resuelve la
tipografía por fontconfig, así que `Arial` sustituye distinto en Windows y en
Ubuntu y los píxeles del subtítulo quemado difieren. La garantía para ellos es
que un run sin cambios no reescribe nada, no que dos máquinas produzcan bytes
idénticos. El `.srt` y el `.ass` sí son idénticos en todas partes.

Ningún artefacto puede contener `NaN`, `Infinity` ni `-Infinity`: no son JSON
estándar y no describen duraciones, posiciones ni probabilidades. Se rechazan en
el borde —configuración, salida del proveedor, modelos de dominio— y `write_json`
los vuelve a rechazar como última barrera.

## Desarrollo

```console
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Las pruebas de integración usan FFmpeg y ffprobe reales sobre material generado
con `lavfi`; no descargan nada ni tocan la red. Se omiten con un motivo explícito
si FFmpeg no está instalado:

```console
uv run pytest -m integration --no-cov
```

`--no-cov` es obligatorio en la ejecución enfocada. La puerta de cobertura
(`--cov-fail-under=80`) mide la suite completa; aplicada solo a las 13 pruebas de
integración da ~68% y haría fallar el comando aunque las 13 pasen. La puerta se
mantiene intacta para `uv run pytest`, que es donde significa algo.

`faster-whisper` es opcional durante el desarrollo. Se instala mediante el extra
`transcription`; `doctor` informa claramente si no está disponible.
