# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`,
  `#3 Stabilize Content Engine V0.1-V0.3` (merge commit `d047479`),
  `#4 Candidate engine foundation` (merge commit `70fb6ed`),
  `#5 Candidate engine deterministic pipeline` (merge commit `1b15e0f`)
- Active branch: `feat/gemini-candidate-provider`, based on `1b15e0f`
- Current package version: `0.1.0`

## Verification baseline

`main` at `1b15e0f` was verified before this branch started: ruff, ruff format
(84 files), mypy strict (40 files), 1086 tests at 99.38%, 13 integration tests
and `uv build`, all green. That is the baseline this branch is measured against.

Last verification, on `feat/gemini-candidate-provider`, Windows 11,
Python 3.12.10, FFmpeg 9.0.1:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 94 files |
| `uv run mypy src` | passed, 43 files, strict |
| `uv run pytest` | 1252 passed, 1 skipped, 166 more than the 1086 on `main` |
| The one skipped test | `tests/ai/test_gemini_live.py`, which spends real quota; it skips unless `CONTENT_ENGINE_RUN_AI_TESTS=1` **and** a credential are both set |
| `uv run pytest` from a working directory outside the repository | passed, no stray files |
| `uv run pytest` at `COLUMNS=40` and `COLUMNS=200` | passed at both; no assertion depends on the console width |
| GitHub Actions on Ubuntu, real FFmpeg | all steps pass (`.github/workflows/ci.yml`) |
| SonarCloud quality gate | passes; Reliability and Security both A |
| `uv run pytest -m integration --no-cov` | 13 passed with real FFmpeg |
| Non-finite numbers refused | 88 parametrised cases for `nan`, `inf`, `-inf` |
| Transcription digests unchanged by the canonical extraction | pinned to `main` at `d047479` and asserted |
| Provider SDK, network client or yt-dlp imported anywhere in `src/` | only `adapters/analysis/gemini_analyzer.py`; asserted by two AST sweeps, plus a test that the one exempted file still exists so a rename cannot silently widen the hole |
| Installed dependency closure | 38 packages; `google-genai` 2.22.0 and its `google-auth` are present by ADR-026, and no other provider SDK, no yt-dlp and no faster-whisper |
| `analyze` end to end from the installed wheel, outside the repository | run reaches ANALYZED, reuse byte-identical, refusal exits 3, `--force` regenerates, analyzer failure exits 5, recovery from FAILED_ANALYSIS |
| Each of the four artifacts edited or deleted in turn | every case refused with exit 3, and every other file plus the manifest left byte-identical |
| Candidate collection identical under 24 permutations of the proposals | asserted |
| Stage configuration coherent with the manifest | fingerprint rebuilt from the artifact matches the recorded one |
| `uv build` | wheel and sdist built; both ship `content_engine/resources/prompts/clip_candidates/v1.txt` beside `default.toml` |
| Prompt identity from the installed wheel | `clip_candidates/v1`, 6656 chars, LF endings, SHA-256 identical to the checkout's |
| `analyze` without a credential, from the wheel | exit 2, no traceback, the message names `GEMINI_API_KEY` and `--fixture`, and the manifest and analysis directory are untouched |
| `analyze --fixture` without a credential, from the wheel | exit 0, recorded as `fixture` with prompt `fake-fixture/v1`, never as Gemini |
| Fixture artifacts offered to the provider, and the reverse | exit 3 both ways, nothing written; no special case, the stage configuration digest decides |
| Credential or credential name anywhere under a run directory | none; searched recursively as bytes |
| Provider reuse with the credential removed | exit 0, reuse reported, no client built, the variable never read, all four artifacts and the manifest byte-identical. Asserted in the unit suite against a stand-in analyzer, **not** from the wheel: producing the provider artifacts to reuse would itself need a real call |
| An unknown `analysis.prompt_version`, from the wheel | exit 2 before the run is touched, in both fixture and provider mode, with no silent reuse of the artifacts the known prompt produced |
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
| Total | 99.45% (2384 statements, 13 missed) |
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

Every module added by CE-026, CE-028 and CE-029 is at 100%:
`adapters/analysis/prompt.py`, `adapters/analysis/structured_output.py` and
`adapters/analysis/gemini_analyzer.py`, as are the CE-030 to CE-033 modules
beside them. The 13 uncovered lines are the same pre-existing ones as before:
the faster-whisper decode loop, `main()` and `__main__`, one transcription
warning branch and two configuration lines. Coverage rose from 99.38% to 99.45%
across the branch.

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

### V0.4 deterministic pipeline — merged in `1b15e0f` (PR #5)

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
- **The `analyze` command**: with `--fixture`, no network, no SDK and no
  credential. A fixture that answers a chunk the run does not have, or fails to
  answer one it does, is refused before the first call.
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
  collection holds, the collection's limits must be the ones the stage ran
  under, and all four must name the same chunking rules version — that last one
  was added after a review found the check compared only two of the four, which
  left a raw collection or a stage configuration free to claim the windows were
  cut by rules that produced something else. The same check runs before anything
  is written.
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

### V0.4 provider — in PR C, pending merge

CE-026, CE-028 and CE-029, in `resources/prompts/clip_candidates/v1.txt`,
`adapters/analysis/prompt.py`, `adapters/analysis/structured_output.py` and
`adapters/analysis/gemini_analyzer.py`. ADR-025 records the prompt's packaging,
ADR-026 the dependency decision, ADR-027 retries and exit codes.

- **CE-026, the prompt.** `clip_candidates/v1`, a packaged resource read through
  `importlib.resources`, so it is found from a checkout, an installed wheel and
  any working directory. Its SHA-256 is taken over the text with line endings
  normalised, so the same prompt has the same identity on Windows and on Linux.
  `analysis.prompt_version` **selects** it: the short name a profile writes
  (`v1`) resolves to the resource, the text sent, the identity recorded
  (`clip_candidates/v1`), the digest, the stage configuration, the fingerprint
  and therefore reuse. An unknown version exits 2 before the run is touched, in
  fixture mode as well as provider mode, and is never resolved to the only one
  that happens to exist.
  It states the safety half explicitly: the transcript is data, instructions
  inside it are never followed, nothing is executed, nothing is invented, no
  total score is returned, virality is never promised, and
  `run_target_candidates` is not a per-chunk quota.
- **CE-029, structured output.** One pair of strict Pydantic models is both the
  schema sent to the provider and the parser applied to what returns, so the
  constraints are stated once and enforced twice. The provider layer owns shape:
  JSON, an object, a known category, integer scores in range, no missing field,
  no extra field, no `total_score`. It deliberately does not own timestamps — a
  negative or inverted interval must reach CE-030 to be measured and recorded as
  invalid, because refusing it here would replace a measurement with a parse
  error. `NaN` and the infinities are the exception: not a timestamp at all.
- **CE-028, the adapter.** `GeminiContentAnalyzer` behind the port, the one file
  in the package permitted to import a provider SDK or a network client. The
  hashed prompt travels as `system_instruction`; the operator's parameters as a
  JSON block; the speech after them inside a delimited block. Tools, tool
  configuration and automatic function calling are switched off explicitly.
- **Two modes that never share artifacts.** `analyze RUN_ID [--fixture PATH]`.
  Nothing special-cases the separation: the stage configuration names whatever
  actually ran and its digest decides reuse, so switching either way is refused
  with exit 3 and nothing written.
- **Failure classification.** A missing credential, a missing SDK or a
  placeholder model is exit 2 and leaves the run untouched — nothing was called,
  so nothing is recorded. A provider failure is exit 5 and `FAILED_ANALYSIS`.
  Retries cover transport errors, 408, 429 and 5xx only, three attempts with a
  2s/8s backoff; a 400, 401, 403 or schema violation is never retried.
- **Identity before construction.** What a run *would* record — analyzer,
  version, model, selected prompt — is computed with no SDK import, no
  environment read, no client and no socket. That identity builds the plan and
  decides reuse, so verifying four finished artifacts never needs a credential:
  a machine that has lost its key can still be asked what a completed run
  contains, and recovery from `FAILED_ANALYSIS` works the same way. Only once
  candidates must actually be produced are the SDK, the model and the credential
  validated and a client built.
- **The manifest names the prompt that was sent.**
  `manifest.versions.prompt_version` and `prompt_sha256` hold the selected
  prompt for a provider run and `null` for a fixture run, and are rewritten in
  both directions by `--force` so they never describe the previous executor.
  That is a narrower question than the stage configuration's field of the same
  name, which records the prompt identity of whatever ran and for which the
  fixture's `fake-fixture/v1` is truthful.
- **Credentials.** `GEMINI_API_KEY` is read only when the real analyzer is
  constructed — which is now the produce path only. A fixture run never consults
  it, a reuse never consults it, and tests assert the variable is not merely
  unused but never looked at. Error messages are rebuilt from the
  status code rather than copied, because the SDK's own `str()` embeds the whole
  decoded response body; the environment is read on the failure path only, to
  redact a key the provider echoed back.

### Configuration levels for the analysis stage

`analysis/config.effective.json` records what the stage really ran: the analyzer
and model that executed, the provider and model the configuration named, the
prompt and fixture identity, the chunking and candidate settings, and every rule
and schema version. `manifest.stages["analysis"]` records the fingerprint, the
digest of that file, the schema version and the completion time, exactly as the
transcription stage does.

## The real video, and where it stopped

A 35-minute Spanish Linux tutorial held locally under `samples-local/`
(ignored by Git, never committed, never sent anywhere) was taken as far as this
machine can take it.

| Step | Result |
|---|---|
| `ffprobe` | 2101.61 s (35.0 min), 640x360, h264 at 23.976 fps, AAC stereo 44.1 kHz, 55.1 MB |
| `content-engine run` | reached `AUDIO_READY`; normalised WAV extracted |
| `content-engine transcribe --config configs/fast.toml` | 1346 segments, 6398 words, language `es`, 618.95 s on cpu/int8, RTF 0.2945, model `small` |
| Chunking, at the packaged 360 s window and 30 s overlap | **7 chunks**, 125 to 392 segments each, 6323 to 16139 characters, 78074 characters in total |
| `content-engine analyze` (provider mode) | **exit 2**: `GEMINI_API_KEY` is not set |

Checked again after the identity/reuse fix, with the credential still absent
from the process, user and machine environments and no `.env` present. The
answer is unchanged and the reason is unchanged.

The refusal was checked rather than assumed: after it, `manifest.json` was
byte-identical to the copy taken beforehand, the run was still `TRANSCRIBED`
with no `failure`, and `analysis/` contained zero files. `doctor --require-ai`
reports the same block as `Analysis credentials FAIL`.

So a real Gemini run on this video would be **7 calls** over roughly 78 000
characters of transcript plus the prompt — an order of magnitude, not a
measurement, because no call was made.

**Nothing about candidate quality is known.** The profile used was `fast`
(`small` model), chosen explicitly and recorded here; it is not the profile a
quality evaluation should use. To finish the experiment:

```bash
setx GEMINI_API_KEY "..."        # or export, in the shell that will run it
uv run content-engine doctor --require-ai
uv run content-engine analyze RUN_ID
```

A second invocation without `--force` must then reuse all four artifacts and
make no call at all, which is the cheap way to confirm the run really happened
once.

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
- **`model` is the identifier requested, not the revision that answered.** The
  response carries `modelVersion`, documented only as "the model version used to
  generate the response"; the published reference does not say whether it
  returns the alias or a dated build, and no call has been made here to observe
  it. The adapter accepts the requested model or a dated variant of it, refuses
  anything else, and records the requested identifier. No schema was widened to
  hold a resolved model, because that needs evidence from a real response that
  this branch does not have. ADR-027 records what would change if a live run
  showed a specific revision.
- `analysis.model` is set to `gemini-3.5-flash-lite`, confirmed against the
  current published model list as stable and available. **It has still never been
  called.** The adapter, the prompt and the parser are complete and verified
  against doubles, but no request has left this machine, because `GEMINI_API_KEY`
  is not set here. Everything below about candidate quality therefore remains
  unmeasured, and the first real call is the outstanding work of PR C.
- `configs/quality.toml` restates the packaged defaults verbatim, so it is a
  no-op overlay. The profiles are not shipped inside the wheel either, so a
  wheel-only installation has no `--config` profile to point at.
- The candidate engine has never seen real model output. Every proposal it has
  processed was written by hand into a fixture or by a test double, so the
  pipeline and the adapter are verified and the *quality* of what they rank is
  entirely unmeasured. CE-026, CE-028 and CE-029 make that measurement possible;
  they do not make it. The specification asks for at least five representative
  videos before candidate quality is treated as known.
- An unparseable provider response is not persisted. `raw_response` is only
  carried on a successful batch and nothing is written until all four artifacts
  are valid, so the malformed reply — the single most useful artifact for
  debugging a prompt — survives only as a bounded excerpt in the error message.
  Keeping it would mean writing a partial artifact, which would break the
  guarantee that a failed stage leaves nothing behind. ADR-027 records the
  trade-off.
- Retry counts and backoff are module constants, not configuration. They change
  how long a failure takes, never what the artifacts contain, so they are
  outside the stage configuration and the fingerprint by design.
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
(CE-023, CE-024, CE-025, CE-027) is merged as `70fb6ed`; the deterministic
pipeline (CE-030–CE-033) and the `analyze` command as `1b15e0f`. CE-026, CE-028
and CE-029 are on `feat/gemini-candidate-provider`, pending review.

That completes CE-023–CE-033 as code. What it does not complete is the reason
the subsystem exists: no real call has been made from this machine, so candidate
quality is still entirely unmeasured. The next work is running real videos
through the finished pipeline and reporting what comes out.

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
