# Stabilization V0.1 to V0.3

Scope: close and verify CE-001 to CE-022. CE-023 onwards, the full V0.7
idempotency system and yt-dlp are explicitly out of scope.

## Completed

| # | Work | CE |
|---|---|---|
| 0 | Repaired `CLAUDE.md` and `docs/CURRENT_STATE.md`; excluded `docs/` from Ruff so `ruff format --check .` passes; ignored `samples-local/` | — |
| 1 | Packaged `default.toml` in `content_engine/resources`, read through `importlib.resources`; removed `project_root()`; workspace resolved against the working directory | CE-007 |
| 2 | Strict configuration: unknown keys rejected, closed value sets as enums, cross-field invariants, `boundary_snap_seconds = 2.5` | CE-007 |
| 3 | Manifest `schema_version`, specification-shaped fields, logical `config_sha256`, LF/UTF-8 artifact contract | CE-010 |
| 4 | Explicit run state machine, failures classified by stage, failed runs preserved and diagnosable | CE-009 |
| 5 | Exception hierarchy with exit codes, central CLI mapping, honest `doctor` with `--require-ai` | CE-008, CE-013 |
| 6 | Transcript normalization under versioned rules, contiguous SRT numbering, resolved hardware recorded | CE-018, CE-021 |
| 7 | CE-022 metrics in `transcript/metrics.json`; `transcribe --force` gated by a transcription fingerprint | CE-017, CE-022 |
| 8 | Real FFmpeg and ffprobe integration tests, skipped with a reason when absent | CE-015 |
| 9 | Coverage threshold, three ADRs, documentation refresh | CE-003, CE-005 |
| 10 | Adversarial review fixes: atomic writes for every artifact, transcription failures exit as transcription, the manifest names the model that actually ran, the suite passes from any working directory | CE-008, CE-010, CE-017 |
| 11 | `NaN` and the infinities refused at every boundary; JSON artifacts cannot carry a non-standard literal | CE-010, CE-018 |
| 12 | `transcript/config.effective.json` and `stage_config_sha256`: run configuration and stage configuration are both recoverable | CE-007, CE-010, CE-017 |
| 13 | State machine tests rewritten against a matrix transcribed from ADR-018 rather than derived from the implementation | CE-009 |
| 14 | `.gitattributes` line-ending policy | — |

## Deferred to V0.7 (CE-047 to CE-052)

The design leaves room for these without changing the domain:

- fingerprints for every stage — `manifest.stages` is already a map;
- a generic stage service — the reuse decision lives in `TranscriptionService`
  with a signature that can be extracted;
- `resume` — the state machine and `StageRecord.completed_at` give it the latest
  valid stage;
- downstream invalidation — `StageRecord.fingerprint` is what CE-052 will compare;
- idempotency for `content-engine run`, which still creates a new run every time.

## Configuration levels

A run holds two configurations and both are needed:

```text
config.effective.json               configuration the run was created with
transcript/config.effective.json    configuration the stage actually executed
```

ADR-017 records why. CE-047–CE-052 will generalise the shape — `manifest.stages`
is a map and `StageRecord` already carries `stage_config_sha256` — but no generic
stage service exists yet and none is invented here.

## Known limitations

- The faster-whisper model call itself is not covered by automated tests; it
  needs a real model. It is verified manually and recorded in `CURRENT_STATE.md`.
- `ANALYZED` onwards are declared in the state machine but unreachable until V0.4
  and V0.6 exist.
- There is no CI yet (CE-064). Integration tests must be run locally with FFmpeg
  installed.
- `metrics.processing_seconds`, and so the RTF, include model download and load.
- The cumulative effect of the 0.05 s tolerance is unbounded in theory; each
  correction is counted, and no real ASR produces the pattern that would trigger
  it.
- ffprobe diagnostics lose non-ASCII path characters on Windows;
  `manifest.input.path` is unaffected.
- `doctor` is not routed through `_execute`, and `_ffmpeg_version` catches a
  redundant exception class.
- No CUDA, no `large-v3`, no long audio, no Linux and no concurrency were
  exercised.
