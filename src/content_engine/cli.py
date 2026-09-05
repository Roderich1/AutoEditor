from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from content_engine.adapters.media.ffmpeg import FFmpegAdapter
from content_engine.adapters.media.ffprobe import FFprobeAdapter
from content_engine.adapters.persistence.filesystem import RunWorkspace
from content_engine.adapters.transcription.faster_whisper import FasterWhisperTranscriber
from content_engine.config import Settings, load_settings
from content_engine.domain.enums import RunStatus
from content_engine.domain.exceptions import (
    ConfigurationError,
    ContentEngineError,
    InvalidMediaError,
    TranscriptionError,
)
from content_engine.services.doctor_service import DoctorService
from content_engine.services.media_service import MediaService
from content_engine.services.run_service import RunService
from content_engine.services.transcription_service import TranscriptionService

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
ConfigOption = Annotated[Path | None, typer.Option("--config", help="TOML profile to merge")]


def _settings(config: Path | None) -> Settings:
    return load_settings(config)


def _workspace(settings: Settings) -> RunWorkspace:
    return RunWorkspace(settings.workspace.root)


def _media_service() -> MediaService:
    return MediaService(FFprobeAdapter(), FFmpegAdapter())


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Check the local environment and optional provider configuration."""
    try:
        checks = DoctorService(_settings(config)).run()
    except ConfigurationError as error:
        console.print(f"[red]Configuration error:[/red] {error}")
        raise typer.Exit(2) from error
    table = Table(title="Content Engine Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in checks:
        status = (
            "[green]OK[/green]"
            if check.ok
            else ("[yellow]WARN[/yellow]" if not check.required else "[red]FAIL[/red]")
        )
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if not all(check.ok for check in checks if check.required):
        raise typer.Exit(1)
    console.print("[green]System ready.[/green]")


@app.command("inspect")
def inspect_media(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
) -> None:
    """Inspect and validate a video using FFprobe."""
    try:
        media = _media_service().inspect(video)
    except ContentEngineError as error:
        console.print(f"[red]Invalid media:[/red] {error}")
        raise typer.Exit(3) from error
    console.print_json(data=media.model_dump(mode="json"))


@app.command("run")
def create_run(
    video: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    config: ConfigOption = None,
) -> None:
    """Create a run, inspect the video, and extract normalized WAV audio."""
    try:
        settings = _settings(config)
        workspace = _workspace(settings)
        run_service = RunService(settings, workspace)
        run_path, manifest = run_service.create(video)
        media_service = _media_service()
        media_service.inspect(video, run_path.joinpath("media", "probe.json"))
        run_service.set_status(run_path, manifest, RunStatus.INSPECTED)
        media_service.extract_audio(video, run_path.joinpath("audio", "source.wav"))
        run_service.set_status(run_path, manifest, RunStatus.AUDIO_READY)
    except ConfigurationError as error:
        console.print(f"[red]Configuration error:[/red] {error}")
        raise typer.Exit(2) from error
    except ContentEngineError as error:
        console.print(f"[red]Cannot create run:[/red] {error}")
        raise typer.Exit(3) from error
    console.print(f"[green]Run ready:[/green] {manifest.run_id}")


@app.command()
def transcribe(
    run_id: Annotated[str, typer.Argument(help="Existing run identifier")],
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing transcript")] = False,
) -> None:
    """Transcribe a run and export JSON, TXT, and SRT."""
    try:
        settings = _settings(config)
        workspace = _workspace(settings)
        run_path = workspace.require(run_id)
        manifest = workspace.read_manifest(run_path)
        transcript_path = run_path.joinpath("transcript", "transcript.json")
        if transcript_path.is_file() and not force:
            console.print("Transcription already completed. Use --force to rerun.")
            return
        audio_path = run_path.joinpath("audio", "source.wav")
        if not audio_path.is_file():
            raise InvalidMediaError(f"Run audio is missing: {audio_path}")
        service = TranscriptionService(FasterWhisperTranscriber())
        transcript = service.transcribe(
            audio_path,
            run_path.joinpath("transcript"),
            settings.transcription,
        )
        RunService(settings, workspace).set_status(run_path, manifest, RunStatus.TRANSCRIBED)
    except ConfigurationError as error:
        console.print(f"[red]Configuration error:[/red] {error}")
        raise typer.Exit(2) from error
    except InvalidMediaError as error:
        console.print(f"[red]Invalid run:[/red] {error}")
        raise typer.Exit(3) from error
    except TranscriptionError as error:
        console.print(f"[red]Transcription failed:[/red] {error}")
        raise typer.Exit(4) from error
    console.print(
        f"[green]Transcript ready:[/green] {len(transcript.segments)} segments, "
        f"language={transcript.language}"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
