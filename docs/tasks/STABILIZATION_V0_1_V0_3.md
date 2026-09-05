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

## Deferred to V0.7 (CE-047 to CE-052)

The design leaves room for these without changing the domain:

- fingerprints for every stage — `manifest.stages` is already a map;
- a generic stage service — the reuse decision lives in `TranscriptionService`
  with a signature that can be extracted;
- `resume` — the state machine and `StageRecord.completed_at` give it the latest
  valid stage;
- downstream invalidation — `StageRecord.fingerprint` is what CE-052 will compare;
- idempotency for `content-engine run`, which still creates a new run every time.

## Known limitations

- The faster-whisper model call itself is not covered by automated tests; it
  needs a real model. It is verified manually and recorded in `CURRENT_STATE.md`.
- `ANALYZED` onwards are declared in the state machine but unreachable until V0.4
  and V0.6 exist.
- There is no CI yet (CE-064). Integration tests must be run locally with FFmpeg
  installed.
