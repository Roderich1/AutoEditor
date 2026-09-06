# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`,
  `#3 Stabilize Content Engine V0.1-V0.3` (merge commit `d047479`),
  `#4 Candidate engine foundation` (merge commit `70fb6ed`)
- Active branch: `feat/candidate-engine-pipeline`, based on `70fb6ed`
- Current package version: `0.1.0`

## Verification baseline

`main` at `70fb6ed` was verified before this branch started: ruff, ruff format
(72 files), mypy strict (35 files), 836 tests at 99.16%, `uv build`, and
GitHub Actions on Ubuntu, all green. That is the baseline this branch is
measured against.

Last verification, on `feat/candidate-engine-pipeline`, Windows 11,
Python 3.12.10, FFmpeg 9.0.1:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 83 files |
| `uv run mypy src` | passed, 40 files, strict |
| `uv run pytest` | 1077 passed |
| `uv run pytest` from a working directory outside the repository | passed, no stray files |
| `uv run pytest` at `COLUMNS=40` and `COLUMNS=200` | passed at both; no assertion depends on the console width |
| GitHub Actions on Ubuntu, real FFmpeg | all steps pass (`.github/workflows/ci.yml`) |
| SonarCloud quality gate | passes; Reliability and Security both A |
| `uv run pytest -m integration --no-cov` | 13 passed with real FFmpeg |
| Non-finite numbers refused | 88 parametrised cases for `nan`, `inf`, `-inf` |
| Transcription digests unchanged by the canonical extraction | pinned to `main` at `d047479` and asserted |
| Provider SDK, network client or yt-dlp imported anywhere in `src/` | none; asserted by an AST sweep |
| Provider SDK in the installed dependency closure | none; the wheel installs 17 packages, none of them a provider |
| `analyze` end to end from the installed wheel, outside the repository | run reaches ANALYZED, reuse byte-identical, refusal exits 3, `--force` regenerates, analyzer failure exits 5, recovery from FAILED_ANALYSIS |
| Each of the four artifacts edited or deleted in turn | every case refused with exit 3, and every other file plus the manifest left byte-identical |
| Candidate collection identical under 24 permutations of the proposals | asserted |
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
| Total | 99.38% (2104 statements, 13 missed) |
| Domain | 100% |
| Services | 100% |
| Adapters | 100% except the faster-whisper decode loop |
| CLI | 99% |
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

Every module added by CE-030 to CE-033 is at 100%: `domain/candidate_rules.py`,
`domain/analysis_rules.py`, `services/analysis_service.py` and
`adapters/analysis/fixture_analyzer.py`. The 13 uncovered lines are all
pre-existing: the faster-whisper decode loop, `main()` and `__main__`, one
transcription warning branch and two configuration lines.

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

Implemented in PR #4, pending merge:

- CE-023 transcript chunker. 360 s windows, 30 s overlap, 330 s stride. Segments
  are atomic and are included whole in every window they overlap, so the segments
  in the overlap appear in two consecutive chunks. Timestamps stay absolute
  everywhere; nothing is rebased. An empty transcript produces no chunks rather
  than an error, and a trailing window already covered by its predecessor is not
  emitted. `analysis/chunks.json` carries `schema_version`, the rules version,
  the window settings and the digest of the transcript it was cut from.
- CE-024 candidate schemas in `domain/candidates.py`. `CandidateScores` has no
  total and forbids extra keys. `RawCandidate` accepts any finite pair of
  timestamps, including negative, zero-length and inverted ones, so CE-030 can
  refuse them with a recorded reason instead of a parse error; `NaN` and the
  infinities stay refused.
- A proposal refused before scoring is an `InvalidCandidate`: the parsed
  proposal plus its reasons, with no interval, no boundary and no deterministic
  total, because it never earned them. `proposed.scores` still holds the six
  ratings the provider supplied. The verbatim response stays on
  `CandidateBatch.raw_response`. Anything that reached scoring is a
  `ValidatedCandidate` where every field is real. `CandidateCollection`
  therefore keeps three lists — `candidates`, `rejected`, `invalid` — split by
  how far a proposal got.
- A reason belongs to the phase that could have decided it. `InvalidCandidate`
  may cite only `PRE_SCORING_REASONS`, and may cite several. A scored record
  carries exactly one terminal reason: `BELOW_MIN_SCORE` or `NOT_IN_TOP_N` when
  rejected, `DUPLICATE` when deduplicated.
- `BoundaryAdjustment` checks that its deltas describe the movement they claim,
  and that a reverted adjustment really restored the proposal with zero deltas
  and unchanged anchors. `ValidatedCandidate` must agree with its own boundary.
- `CandidateCounts` are terminal outcomes, mutually exclusive by construction:
  `invalid`, `below_min_score`, `deduplicated`, `not_in_top_n` and `selected`
  sum to `proposed`. Each one is counted from the records that hold it, not
  merely balanced against the others, so two categories cannot be swapped while
  the total still adds up.
- Every `DeduplicationEvent` is checked against the two candidates it names: the
  dropped one is recorded as deduplicated and dropped exactly once, the keeper
  is one that neither deduplication nor the score filter had already removed,
  both scores are the totals on record, and the overlap is recomputed from the
  intervals and must reach `dedupe_iou`. Equal scores are broken by the earlier
  start and then by the identifier.
- CE-025 deterministic score, ADR-008 weights, `Decimal` with `ROUND_HALF_UP`.
- CE-027 `ContentAnalyzerPort` as a Protocol only. No implementation, no SDK.
  `AnalysisContext.run_target_candidates` is named for its scope: the objective
  belongs to the whole run and is never a per-call quota. A per-chunk budget, if
  one is ever needed, is a separate computed field rather than a reinterpretation
  of this one.
- `utils/canonical.py`, the single serialization behind every digest.
- ADR-019: Gemini is the initial analysis provider.

CE-023, CE-024, CE-025 and CE-027 are merged into `main` as part of `70fb6ed`.

### V0.4 deterministic pipeline — in PR B, pending merge

CE-030 to CE-033 and the `analyze` command, in `domain/candidate_rules.py`,
`domain/analysis_rules.py`, `services/analysis_service.py` and
`adapters/analysis/fixture_analyzer.py`. ADR-022 records the binding rules,
ADR-023 the fixture executor.

- **Pipeline order is binding**: validation, snapping, score, minimum-score
  filter, global deduplication, global ranking, ceiling. That order is what makes
  the five terminal outcomes mutually exclusive.
- **CE-030** collects every applicable reason in a canonical order rather than
  the first one found, and skips the duration rules when there is no positive
  duration to judge. Grounding is formalised: an instant is grounded when it
  falls inside a segment or word interval, or lies within `boundary_snap_seconds`
  of a real edge. Arithmetic only — no similarity, no heuristic, no model.
- **CE-031** snaps to the nearest admissible edge, preferring a segment edge over
  a word edge and, on a remaining tie, the earliest for a start and the latest
  for an end. An adjustment that would leave the interval inverted, past the end
  of the source or outside the duration policy is reverted whole.
- **CE-032** computes interval IoU and deduplicates greedily over one priority
  order, globally across chunks, after the minimum-score filter. Every drop is
  recorded as a `DeduplicationEvent` the collection re-verifies against both
  candidates it names.
- **CE-033** ranks with that same order and applies `max_candidates` once. A
  total exactly at `min_score` survives. `target_candidates` is recorded and cuts
  nothing.
- **Identity**: a candidate's id is computed before snapping from the transcript
  digest, the chunk, the ordinal within its batch, the proposal and the prompt
  identity, so two identical proposals from two overlapping chunks stay two
  records.
- **The `analyze` command**: `analyze RUN_ID --fixture PATH [--config PATH]
  [--force]`. No network, no SDK, no credential. A fixture that answers a chunk
  the run does not have, or fails to answer one it does, is refused before the
  first call.
- **Four artifacts**: `analysis/chunks.json`, `analysis/candidates.raw.json`,
  `analysis/config.effective.json` and `analysis/candidates.json`, all computed
  and validated in memory first, then written atomically as UTF-8 with LF
  endings.
- **Reuse proves all four artifacts.** An independent review of PR #5 found
  that the first version proved two: `chunks.json` was never read back, on the
  argument that it could be rebuilt from the transcript, and the fingerprint
  covered only the raw batches — leaving `candidates.json` and the identity
  fields above those batches trusted with no evidence. Both are now covered.

  A reuse is accepted only when all four files are present, readable and valid
  under their own schemas; the stage configuration digest recomputes to what the
  manifest recorded; the four agree with each other and with the current
  transcript; the chunks on disk are the ones this transcript and these settings
  produce; the fingerprint rebuilds from all four plus the transcript; and the
  configuration being asked for now is the one recorded. Every refusal exits 3
  and writes nothing; `--force` replaces the artifacts atomically.

  The fingerprint hashes the whole of each artifact rather than a chosen subset,
  and its version is 2. This departs from the transcription stage, whose
  fingerprint covers inputs only: that stage protects one artifact which is read
  back and validated in full, while a digest over inputs alone cannot see an
  edited output among four. The consequence is that it is an integrity digest
  over one execution rather than a portable identity — two runs of identical
  inputs differ, because `generated_at` is inside it. `config_sha256` and
  `stage_config_sha256` remain the portable identities.
- **The artifacts must agree with one another.** A fingerprint proves the four
  files were written together; it cannot prove they were coherent when written,
  nor that they still describe the transcript in front of them — a set moved
  from another run would be internally consistent and completely wrong. Both
  collections must name the transcript the run holds, the raw collection and the
  stage configuration must agree on the analyzer, its version, the model and the
  prompt and fixture identity, the batches must answer exactly the chunks on
  disk, the funnel's `counts.proposed` must match the proposals the raw
  collection holds, and the collection's limits must be the ones the stage ran
  under. The same check runs before anything is written.
- **A verified reuse settles the run's status.** A failed `--force` leaves the
  earlier artifacts untouched, so a later invocation that proves they still
  match every input is looking at a completed stage. If the run is already
  `ANALYZED` nothing is rewritten and reuse stays byte-identical down to the
  manifest; if it is `FAILED_ANALYSIS` the run advances to `ANALYZED`, the
  failure is cleared, the verified stage record is kept, and the command reports
  a recovery rather than a plain reuse. A refusal recovers nothing.
- **One executor per run.** A `RawCandidateBatch` cannot name a model its
  collection does not, and the service refuses an analyzer that answers under a
  name other than the identity every artifact records. A fixture batch cannot
  carry both a recorded failure and candidates; it may keep the response beside
  an error, which is the most useful thing about a failure.

Not implemented yet: CE-026 prompt `clip_candidates/v1`, CE-028 Gemini adapter,
CE-029 structured output parsing. Those are PR C.

### Configuration levels for the analysis stage

`analysis/config.effective.json` records what the stage really ran: the analyzer
and model that executed, the provider and model the configuration named, the
prompt and fixture identity, the chunking and candidate settings, and every rule
and schema version. `manifest.stages["analysis"]` records the fingerprint, the
digest of that file, the schema version and the completion time, exactly as the
transcription stage does.

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
- The candidate engine has never seen real model output. Every proposal it has
  processed was written by hand into a fixture, so the pipeline is verified and
  the *quality* of what it ranks is entirely unmeasured. That measurement needs
  CE-026 and CE-028.
- Grounding is coarse by design. A timestamp anywhere inside a long monologue is
  grounded, because it falls inside a segment. The rule refuses timestamps the
  transcript cannot support at all; it does not judge whether the moment is good.
- The spec's `analysis/raw/chunk_*.json` per-chunk files are not written.
  `analysis/candidates.raw.json` is the canonical aggregate: one batch per
  chunk, each with its verbatim response, plus the identity of what produced
  them. `RunWorkspace.create` does create `analysis/raw/`, so that directory
  exists on any run made by `content-engine run` and this stage leaves it empty.
  Out of scope for now and recorded rather than resolved silently.
- Deduplication is greedy, not optimal. It keeps the best candidate of each
  cluster in priority order, which is what ADR-009 asks for; it does not search
  for the globally best set of non-overlapping candidates.
- A candidate identifier is a 64-bit prefix of a SHA-256. Collisions are refused
  by the collection rather than resolved, so an astronomically unlikely one would
  be a loud failure rather than a lost record.

## Current priority

V0.4, the Candidate Intelligence Engine (CE-023–CE-033). The foundation
(CE-023, CE-024, CE-025, CE-027) is merged as `70fb6ed`. The deterministic
pipeline (CE-030–CE-033) and the `analyze` command are on
`feat/candidate-engine-pipeline`, pending review.

Next is PR C: CE-026 `clip_candidates/v1`, CE-028 the Gemini adapter and CE-029
structured-output parsing, which is the first change that will make a real call
and the first that can say anything about candidate quality.

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
