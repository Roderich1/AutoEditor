# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`
- Active branch: `chore/stabilize-v0-1-v0-3` (not yet pushed)
- Current package version: `0.1.0`

## Verification baseline

Last verification, on `chore/stabilize-v0-1-v0-3`, Windows 11, Python 3.12.10,
FFmpeg 9.0.1:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 59 files |
| `uv run mypy src` | passed, 30 files, strict |
| `uv run pytest` | 278 passed |
| `uv run pytest -m integration` | 13 passed with real FFmpeg |
| `uv build` | wheel and sdist built |
| Wheel in a clean venv, arbitrary working directory | `doctor`, `inspect`, `run`, `transcribe` all work |
| Real faster-whisper transcription | passed, model `tiny`, 3 s lavfi fixture |
| Artifacts UTF-8 without BOM, LF endings | verified byte by byte |
| Secrets or media tracked in Git | none; tracked tree is 710 KB |

Coverage:

| Scope | Coverage |
|---|---|
| Total | 99% (1084 statements, 14 missed) |
| Domain | 100% |
| Services | 100% |
| CLI | 98% |
| faster-whisper adapter | 79% |

`--cov-fail-under=80` is enabled.

Files with meaningful uncovered lines:

- `adapters/transcription/faster_whisper.py` lines 53-72, the model load and
  decode loop. Covering it needs a real model, so it is verified manually
  instead. Hardware resolution and segment translation around it are covered.

This baseline must be updated after every milestone or PR.

## Milestone status

### V0.1 Foundation — stabilized

- Configuration loads from the packaged `content_engine/resources/default.toml`
  through `importlib.resources`, from a checkout, an installed wheel or any
  working directory. Defaults exist in exactly one place.
- Unknown keys, closed value sets and cross-field invariants are enforced with
  messages that name the offending key or relation.
- `manifest.json` carries `schema_version`; an unknown schema is refused.
- `config_sha256` is the logical configuration hash and is identical across
  machines; `config.effective.json` keeps the full configuration for diagnosis.
- `RunStatus` has an explicit, tested state machine. Failures are classified by
  stage and the failed run is kept with its reason.
- `doctor` reports the real configuration layers and accepts `--require-ai`.

### V0.2 Media — stabilized

- ffprobe failing on a corrupt file is reported as invalid media; a missing
  FFmpeg stays a configuration problem with exit code 2.
- An undeclared frame rate no longer rejects an otherwise valid source.
- Subprocesses have timeouts and FFmpeg no longer inherits stdin.
- CE-015 integration tests verify the extracted WAV really is mono 16 kHz
  `pcm_s16le` with the source duration.

### V0.3 Transcription — stabilized

- Provider output is normalized under versioned rules. Whitespace, empty segments
  and differences within 0.05 s are corrected and counted; material disorder,
  words outside their segment, negative intervals and timestamps past the real
  audio duration are rejected.
- The real audio duration is measured with ffprobe and compared against the one
  the transcriber declares.
- SRT numbering is contiguous.
- CE-022 metrics are written to `transcript/metrics.json`.
- `transcribe --force`: hardware is resolved before the reuse decision, and a
  transcript is reused only when its fingerprint matches.

### V0.4 Candidate Intelligence Engine

Status: not started. Scope: CE-023 through CE-033.

## Current priority

Review and merge the stabilization PR. Then begin CE-023–CE-033 in a separate
branch and PR.

## Deferred to V0.7 (CE-047 to CE-052)

Per-stage fingerprints beyond transcription, a generic stage service, `resume`,
downstream invalidation, and idempotency for `content-engine run`, which still
creates a new run on every invocation. `manifest.stages` is already a map so
these can be added without changing the domain.

## yt-dlp decision

yt-dlp remains outside the V0 core.

It may be used manually to obtain authorized local experiment videos. It must
not be required by unit tests, integration tests or CI. A future product
integration requires a separate ADR.
