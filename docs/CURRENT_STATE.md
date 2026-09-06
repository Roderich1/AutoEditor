# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`,
  `#3 Stabilize Content Engine V0.1-V0.3` (merge commit `d047479`)
- Active branch: `feat/candidate-engine-foundation`
- Current package version: `0.1.0`

## Verification baseline

Last verification, on `feat/candidate-engine-foundation`, Windows 11,
Python 3.12.10, FFmpeg 9.0.1:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 66 files |
| `uv run mypy src` | passed, 30 files, strict |
| `uv run pytest` | 762 passed |
| `uv run pytest` from a working directory outside the repository | 762 passed, no stray files |
| `uv run pytest` at `COLUMNS=40` and `COLUMNS=200` | 762 passed at both; no assertion depends on the console width |
| GitHub Actions on Ubuntu, real FFmpeg | all steps pass (`.github/workflows/ci.yml`) |
| SonarCloud quality gate | passes; Reliability and Security both A |
| `uv run pytest -m integration --no-cov` | 13 passed with real FFmpeg |
| Non-finite numbers refused | 88 parametrised cases for `nan`, `inf`, `-inf` |
| Transcription digests unchanged by the canonical extraction | pinned to `main` at `d047479` and asserted |
| Provider SDK imported anywhere in `src/` | none; asserted by an AST sweep |
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
| Total | 99% (1415 statements, 13 missed) |
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
- Reuse re-reads `transcript/config.effective.json` and rebuilds both the stage
  hash and the fingerprint from it. A missing, corrupt, mis-schemaed, edited or
  mismatched artifact is refused with exit code 3 and nothing is written.
- `manifest.json` is at schema 2. A manifest written by an earlier build is
  refused rather than reinterpreted, which is the documented behaviour.

### V0.4 Candidate Intelligence Engine — in progress

Scope: CE-023 through CE-033, in three pull requests. The first is the
deterministic foundation and adds no provider call.

Landed:

- CE-023 transcript chunker. 360 s windows, 30 s overlap, 330 s stride. Segments
  are atomic and are included whole in every window they overlap, so the segments
  in the overlap appear in two consecutive chunks. Timestamps stay absolute
  everywhere; nothing is rebased. An empty transcript produces no chunks rather
  than an error, and a trailing window already covered by its predecessor is not
  emitted. `analysis/chunks.json` carries `schema_version`, the rules version,
  the window settings and the digest of the transcript it was cut from.
- CE-024 candidate schemas in `domain/candidates.py`. `CandidateScores` has no
  total and forbids extra keys. `RawCandidate` is untrusted by construction.
  Rejected and deduplicated candidates are kept with their reasons.
  `CandidateCollection` refuses to exceed `max_candidates`.
- CE-025 deterministic score, ADR-008 weights, `Decimal` with `ROUND_HALF_UP`.
- CE-027 `ContentAnalyzerPort` as a Protocol only. No implementation, no SDK.
- `utils/canonical.py`, the single serialization behind every digest.
- ADR-019: Gemini is the initial analysis provider.

Not implemented yet: CE-026 prompt, CE-028 Gemini adapter, CE-029 structured
output, CE-030 to CE-033, and the `analyze` command.

### Configuration levels for the analysis stage

`analysis/config.effective.json` and `manifest.stages["analysis"]` will follow
the pattern the transcription stage already uses. Neither exists yet; the shape
they need does, because `manifest.stages` is a map and `StageRecord` already
carries `stage_config_sha256`.

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
- `analysis.model` is set to `gemini-3.5-flash-lite`, the model ADR-019 plans to
  use. It has not been exercised against a live API, because this change adds no
  adapter and makes no call.
- `configs/quality.toml` restates the packaged defaults verbatim, so it is a
  no-op overlay. The profiles are not shipped inside the wheel either, so a
  wheel-only installation has no `--config` profile to point at.

## Current priority

V0.4, the Candidate Intelligence Engine (CE-023–CE-033). The foundation is on
`feat/candidate-engine-foundation`. Next is the deterministic pipeline
(CE-030–CE-033) with the `analyze` command driven by a fake analyzer, then the
prompt and the Gemini adapter.

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
