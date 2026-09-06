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
| `uv run ruff format --check .` | passed, 61 files |
| `uv run mypy src` | passed, 30 files, strict |
| `uv run pytest` | 595 passed |
| `uv run pytest` from a working directory outside the repository | 595 passed, no stray files |
| `uv run pytest -m integration --no-cov` | 13 passed with real FFmpeg |
| Non-finite numbers refused | 88 parametrised cases for `nan`, `inf`, `-inf` |
| Stage configuration coherent with the manifest | fingerprint rebuilt from the artifact matches the recorded one |
| `uv build` | wheel and sdist built; the wheel ships `content_engine/resources/default.toml` |
| Wheel in a clean venv, arbitrary working directory with spaces and non-ASCII characters | `doctor`, `inspect`, `run`, `transcribe` all work |
| Real faster-whisper transcription | passed, model `small` on cpu/int8, 34 s of Spanish technical speech |
| Artifacts UTF-8 without BOM, LF endings | verified byte by byte |
| Secrets or media tracked in Git | none; tracked tree is 710 KB |

### Real-speech smoke test

`samples-local/smoke-spanish-technical.mp4` (34.2 s, git-ignored, never
committed), transcribed from the installed wheel, in a directory outside the
repository whose name contains spaces and non-ASCII characters, with
`configs/fast.toml` on a run created under the defaults: language `es` at 0.96,
4 segments, 54 words, contiguous SRT numbering, every word inside its segment,
last timestamp 32.31 s against a 34.23 s audio duration, RTF 0.22 once the model
is cached. A second invocation reused the transcript and left
`transcript/config.effective.json` byte-identical; changing `beam_size` was
refused as incompatible and also left it untouched; `--force` regenerated both
the transcript and the stage configuration. No artifact contained `NaN`,
`Infinity` or `-Infinity`, and no `.tmp` survived.

Accuracy is usable but not clean on technical vocabulary: the `small` model
rendered *systemctl* as "el sistema CETELE" and *compruebo* as "comprego".
General Spanish ("configurando un servidor Ubuntu", "el servicio SSH está
activo", "revisar el firewall") was correct. This is evidence for CE-016–CE-022
plumbing, not evidence that `small` is an adequate production model for
technical content.

Coverage:

| Scope | Coverage |
|---|---|
| Total | 99% (1145 statements, 14 missed) |
| Domain | 100% |
| Services | 100% |
| CLI | 98% |
| faster-whisper adapter | 79% |

`--cov-fail-under=80` is enabled and measures the whole suite.

The focused integration run needs `--no-cov`. The gate applies to whatever
selection pytest was given, and 13 integration tests exercise about 68% of the
package on their own, so `uv run pytest -m integration` fails on coverage even
when all 13 pass. Lowering the threshold would weaken the gate where it actually
means something, so the focused command opts out of measurement instead.

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
- A `--config` profile that differs from the one the run was created under is
  reported rather than applied silently, and
  `manifest.versions.transcription_model` names the model that actually produced
  the transcript.
- A faster-whisper failure exits with the transcription code, not the analysis
  one.
- Every run artifact, `transcript.txt` and `transcript.srt` included, is written
  atomically; a failed write leaves no partial file and no `.tmp`.
- The stage writes `transcript/config.effective.json` with what it actually ran,
  including the resolved device and compute type, and
  `manifest.stages.transcription.stage_config_sha256` ties the manifest to it.
  The run-level `config.effective.json` still records the configuration the run
  was created with. Verified on the real sample: the recorded fingerprint is
  reconstructible from the stage configuration artifact.
- `NaN`, `Infinity` and `-Infinity` are refused wherever a real number is
  required: configuration, ffprobe output, provider output, every domain model,
  and `write_json` as a last defence.
- `manifest.json` is at schema 2. A manifest written by an earlier build is
  refused rather than reinterpreted, which is the documented behaviour.

### V0.4 Candidate Intelligence Engine

Status: not started. Scope: CE-023 through CE-033.

## Known limitations

- `metrics.processing_seconds`, and therefore the RTF, measure the whole
  transcriber call: model resolution, download on first use and load are
  included. The first run of a new model reported RTF 1.91 and the next 0.21 on
  the same audio. RTF is comparable only between runs with a warm model cache.
  Separating load from decode requires a change to `TranscriberPort` and is left
  for when the metric is actually used to choose a model.
- On Windows, ffprobe writes its diagnostics in the console codepage, so
  non-ASCII characters in a source path are replaced with U+FFFD inside
  `manifest.failure.message`. `manifest.input.path` is unaffected and remains
  exact.
- `configs/quality.toml` restates the packaged defaults verbatim, so it is a
  no-op overlay. The profiles are not shipped inside the wheel either, so a
  wheel-only installation has no `--config` profile to point at.

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
