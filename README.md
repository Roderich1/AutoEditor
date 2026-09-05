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
```

`doctor --require-ai` convierte las credenciales y el modelo de análisis en
requisitos obligatorios; por defecto son advertencias, porque la etapa de
análisis todavía no existe.

## Configuración

Los valores predeterminados viven dentro del paquete, en
`content_engine/resources/default.toml`, y se leen con `importlib.resources`.
Esa es la única copia canónica: los modelos Pydantic validan tipos e invariantes
pero no repiten los valores. La configuración funciona igual desde el repositorio,
desde un wheel instalado y desde cualquier directorio de trabajo.

Los perfiles de `configs/` son overlays que se fusionan sobre esos valores:

```console
uv run content-engine run sample.mp4 --config configs/quality.toml
```

Una clave desconocida o una relación inválida se rechazan indicando exactamente
qué falla, en lugar de ignorarse en silencio.

Variables de entorno:

| Variable | Efecto |
|---|---|
| `CONTENT_ENGINE_WORKSPACE` | Raíz del workspace; tiene prioridad sobre el TOML |
| `CONTENT_ENGINE_ANALYSIS_MODEL` | Modelo de análisis |
| `OPENAI_API_KEY` | Credenciales de análisis (todavía sin uso en V0.1–V0.3) |

Un `workspace.root` relativo se resuelve contra el directorio actual, nunca
contra el directorio de instalación. `doctor` y `run` imprimen siempre la ruta
absoluta resuelta.

## Ejecuciones

Cada ejecución vive en `workspace/runs/RUN_ID` y es un experimento:

- `manifest.json` — estado, hashes, versiones y etapas completadas
- `config.effective.json` — configuración completa, para diagnóstico
- `media/probe.json`, `audio/source.wav`
- `transcript/` — `transcript.json`, `.txt`, `.srt` y `metrics.json`

El `run_id` identifica una ejecución; `config_sha256` identifica el experimento y
es idéntico entre máquinas. `transcribe` reutiliza un transcript solamente cuando
su fingerprint coincide con el audio y las opciones actuales, incluido el hardware
realmente resuelto; si no coincide, lo rechaza y explica por qué en lugar de
mezclar artefactos incompatibles. `--force` regenera.

Una ejecución que falla conserva su directorio, su estado `FAILED_*` y el motivo,
para poder diagnosticarla.

Todas las rutas se construyen con `pathlib.Path` y los artefactos se escriben en
UTF-8 con saltos LF, por lo que el mismo código produce los mismos bytes en
Windows 11 y Ubuntu 24.04.

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
uv run pytest -m integration
```

`faster-whisper` es opcional durante el desarrollo. Se instala mediante el extra
`transcription`; `doctor` informa claramente si no está disponible.
