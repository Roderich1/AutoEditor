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

Los artefactos de cada ejecución se guardan bajo `workspace/runs/RUN_ID`. Todas
las rutas se construyen con `pathlib.Path`, por lo que el mismo código funciona
en Windows 11 y Ubuntu 24.04.

## Desarrollo

```console
uv sync --group dev
uv run ruff check .
uv run mypy src
uv run pytest
```

`faster-whisper` es opcional durante el desarrollo. Se instala mediante el extra
`transcription`; `doctor` informa claramente si no está disponible.
