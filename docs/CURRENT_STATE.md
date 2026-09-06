# Current Project State

## Repository

- Repository: `Roderich1/AutoEditor`
- Default branch: `main`
- Merged pull requests: `#1 Proyecto base`, `#2 documentacion del proyecto base`,
  `#3 Stabilize Content Engine V0.1-V0.3` (merge commit `d047479`),
  `#4 Candidate engine foundation` (merge commit `70fb6ed`),
  `#5 Candidate engine deterministic pipeline` (merge commit `1b15e0f`),
  `#6 Candidate engine provider` (merge commit `5570531`)
- Active branch: `feat/human-evaluation`, based on `5570531`
- Current package version: `0.1.0`

## Verification baseline

`main` at `5570531` was reproduced before this branch started: ruff, ruff
format (94 files), mypy strict (43 files), 1252 tests at 99.45% over 2384
statements with 13 missed, 13 integration tests and `uv build`, all green. That
is the baseline this branch is measured against.

Last verification, on `feat/human-evaluation`, Windows 11, Python 3.12.10,
FFmpeg 9.0.1:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed, 111 files |
| `uv run mypy src` | passed, 50 files, strict |
| `uv run pytest` | 1765 passed, 1 skipped, 513 more than the 1252 on `main`; 99.61% over 3314 statements, 13 missed — the same 13 `main` already had |
| The one skipped test | `tests/ai/test_gemini_live.py`, which spends real quota; it skips unless `CONTENT_ENGINE_RUN_AI_TESTS=1` **and** a credential are both set |
| `uv run pytest` from a working directory outside the repository | passed, no stray files |
| `uv run pytest` at `COLUMNS=40` and `COLUMNS=200` | passed at both; no assertion depends on the console width |
| GitHub Actions on Ubuntu, real FFmpeg | all steps pass (`.github/workflows/ci.yml`) |
| Re-run with every cache disabled | `ruff --no-cache`, `mypy --no-incremental`, `pytest -p no:cacheprovider`: all green |
| Every test import available in the CI environment | asserted against the `uv.lock` closure of the main dependencies and the dev group, per test module |
| Publication failure injected at each step, shortlist same, grown and shrunk | the previous set stays byte-identical and still passes `verify_previews` |
| Failure of the restore itself, on the first, a middle and the last file | the whole previous set stays reachable in `previews/` or `previews/.rollback/`, and the backup is never deleted |
| Restore failure repeated three times | the backup is still complete after each attempt |
| A pending backup on a later invocation | restored deterministically from its journal, or refused untouched when the journal is missing, unreadable, of another schema, or names an unknown phase |
| A restore interrupted after 2 of 5 files, then resumed by `resolve_pending_rollback` | the previous set comes back byte-identical and `verify_previews` passes; asserted for the second, a middle and the last move, and for the shortlist unchanged, grown and shrunk |
| Four resumes each stopping at a different position | nothing lost at any step, complete at the end |
| The phase transition itself failing | the journal stays at `placing`, the previous set is whole in the backup, and a resume completes |
| `preview_service.py` coverage | 100% |
| Diff against `main` | 27 files, +8122/−46 |
| SonarCloud quality gate | passes; Reliability and Security both A |
| `uv run pytest -m integration --no-cov` | 22 passed with real FFmpeg; 9 of them are the new preview pipeline |
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
| Secrets or media tracked in Git | none |
| `preview` on the real analysed run | 15 previews in 26 s, every one verified independently with ffprobe |
| `preview --force` on the real run after the transactional publish | 15 regenerated in 26 s, all verified, no `.rollback`, `.staging` or `.tmp` left, third invocation reused |
| Second `preview` on the same run | reuse in 0.49 s, no encode, all 16 files and the manifest byte-identical |
| The four analysis artifacts after previewing | SHA-256 unchanged; analysis fingerprint still `b6f2c48acd57` |
| Wheel in a clean Python 3.12 venv, working directory with a space and `ñ` | `preview --force` produced all 15, a second call reused them, a full `review` session reached REVIEWED |
| A deleted preview on a run whose manifest records the stage | exit 3, nothing rewritten, message names `--force` |
| Credential or credential name under a reviewed run directory | none; searched recursively as bytes |
| `.tmp` or `.staging` left anywhere after a completed run | none |
| Third-party imports in the seven new modules | `pydantic` only; no network client, no provider SDK, no yt-dlp, and no module reads the environment |

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

### V0.4 provider — merged in `5570531` (PR #6)

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
- **CE-029, structured output.** Two related pieces, deliberately not one
  object.

  `provider_response_schema()` builds the **transport schema**: the compatible
  subset that is actually sent to `generateContent`. It carries types, the
  category enum, required fields and their ordering, and nothing else. It exists
  because handing the Pydantic models to the SDK produced a request the API
  rejects outright — `extra="forbid"` serialises to `additionalProperties`,
  which is not in the accepted subset — and swept this project's own docstrings
  into `description` fields, shipping internal commentary to the model inside
  every request.

  `ProviderResponse`, `ProviderCandidate` and `ProviderScores` are the **parser**,
  and they validate strictly whatever comes back: an object, a known category,
  integer scores within 0–100, string lengths, no missing field, no extra field,
  no `total_score`, no `NaN` or infinity.

  The two are kept in step by derivation and by tests rather than by being the
  same object. The transport schema takes its field names from
  `ProviderScores.model_fields` and its categories from `ClipCategory`, and a
  test asserts it lists exactly the fields the parser expects. **The constraints
  are therefore not stated once.** Bounds, string lengths, the rejection of extra
  fields and the rejection of non-finite numbers are enforced *only* locally, on
  the answer, because the wire format cannot express them or rejects them. A
  provider that ignores the schema is caught by the parser, which is the layer
  that has to be trusted regardless.

  Timestamps are owned by neither — a negative or inverted interval must reach
  CE-030 to be measured and recorded as invalid, because refusing it here would
  replace a measurement with a parse error. `NaN` and the infinities are the
  exception: not a timestamp at all.
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

### V0.5 Human Evaluation — CE-034 to CE-039, on `feat/human-evaluation`

Previews and human review, in `domain/previews.py`, `domain/preview_rules.py`,
`domain/review.py`, `ports/preview.py`, `adapters/media/preview.py`,
`services/preview_service.py`, `services/review_service.py` and two new CLI
commands. ADR-028 records why preview encoding is a stage constant, ADR-029 why
a decision is three types rather than one with optional fields, and ADR-030 the
single backwards transition in the state machine.

- **CE-034, previews.** `content-engine preview RUN_ID` cuts one 540x960 proxy
  per selected candidate: H.264 and AAC, `veryfast` at CRF 30, the whole source
  frame fitted inside the vertical frame and padded with `setsar=1` so nothing
  is stretched or cropped. No subtitles and no final styling — those are
  CE-040 to CE-045. The FFmpeg argument list is built by a pure function and
  asserted element by element; `-ss` precedes `-i` so the encoder seeks instead
  of decoding up to the interval, and `-t` follows it so the limit applies to
  what is written.

  Every encode happens in `previews/.staging/` and is read back with ffprobe
  there — dimensions, codecs and duration against a documented 1.0 s tolerance.
  Nothing is moved into `previews/` until the whole set has passed, so a
  failure leaves the previous previews intact and a failed `--force` destroys
  nothing. The index and the stage configuration are written last, and the
  index the reuse check looks for first is written after everything else.

- **CE-035 to CE-039, review.** `content-engine review RUN_ID` shows one
  candidate at a time with its rank, identifier, topic, category, interval,
  duration, total, the six component scores, hook, summary, reason and preview
  path, then five keys: `[A]` approve, `[R]` reject, `[E]` edit range, `[S]`
  skip, `[Q]` quit and save. Each explicit decision is written atomically
  before the next candidate is shown.

  Skipping records nothing, which is what makes "not decided yet" and "decided
  to do nothing" different states: a skipped candidate is pending again next
  session. `Q`, end of input and Ctrl+C all end the session without touching
  the status or the failure record. The run reaches REVIEWED only when every
  selected candidate has an explicit decision.

- **The artifacts.** `previews/index.json` (candidate id, rank, interval,
  expected and measured duration, filename, dimensions, codecs, SHA-256, size,
  plus the analysis fingerprint and source digest they were cut from),
  `previews/config.effective.json`, `review/decisions.json` and
  `review/config.effective.json`. The manifest records a `preview` and a
  `review` stage exactly as transcription and analysis do: a fingerprint, the
  digest of the stage configuration beside the output, a schema version and a
  completion time.

- **Reuse.** The preview fingerprint covers the index and the configuration,
  and the index holds a digest of every file, so it covers the output. A
  deleted, truncated, replaced or renamed preview cannot be reused; nor can a
  set produced under other dimensions, other preview rules, another analysis or
  another source. Verification writes nothing, and a refusal names `--force`.

- **What is deliberately not here.** No SRT or ASS, no final render, no
  `vertical_blur` or `vertical_crop`, no CE-040 and beyond, no second Gemini
  call, no prompt change and no scoring change. `preview` and `review` never
  read `GEMINI_API_KEY` and open no socket.

## The real video: the first real Gemini run

A 35-minute Spanish Linux tutorial held locally under `samples-local/` (ignored
by Git, never committed, never sent anywhere — Gemini receives transcript
chunks, never media).

| Step | Result |
|---|---|
| `ffprobe` | 2101.61 s (35.0 min), 640x360, h264 at 23.976 fps, AAC stereo 44.1 kHz, 55.1 MB |
| `content-engine run` | reached `AUDIO_READY`; normalised WAV extracted |
| `content-engine transcribe --config configs/fast.toml` | 1346 segments, 6398 words, `es`, 618.95 s on cpu/int8, RTF 0.2945, model `small` |
| Chunking, 360 s window and 30 s overlap | **7 chunks**, 125–392 segments each, 78 074 characters in total |
| `content-engine analyze RUN_ID --config configs/fast.toml` | **exit 0 in 202 s**, one call per chunk, no retries, no `--force` |
| Second `analyze`, credential present | exit 0 in 0.5 s, "Candidates reused", all four artifacts and the manifest byte-identical |
| Second `analyze`, credential removed from the process | exit 0 in 0.48 s, same reuse, byte-identical — a call is impossible without a key, so this is the proof of zero calls |

Recorded by the run: status `ANALYZED`, provider `gemini`, model
`gemini-3.5-flash-lite`, `prompt_version` `clip_candidates/v1`, `prompt_sha256`
`557e5539…`, `fixture_sha256` null, fingerprint `b6f2c48acd57`. All four
artifacts UTF-8 without BOM and LF. All seven raw responses parse as JSON. Every
selected candidate lies inside the source **and** inside its own chunk. Neither
the credential nor the string `GEMINI_API_KEY` appears in any of the 13 files
under the run, and `workspace/runs/` is ignored, so nothing reached Git.

### Consumption

| | |
|---|---|
| Calls | 8 — 7 for the production, 1 for the opt-in live test |
| Retries | 0 |
| Wall time | 202 s for the seven, ~29 s per chunk |
| Tokens, measured | only for the opt-in call: 1771 in, 5 out. **The command does not report tokens**, so the seven production calls are unmeasured |
| Cost | **not calculable here**. The API returns token counts, not prices, and the command does not surface them |
| `modelVersion` reported | `gemini-3.5-flash-lite` on every call — the alias, with no revision suffix |

### The funnel

```text
proposed          23      across 7 chunks (1 to 5 per chunk)
invalid            2      1 too_long (106.4 s), 1 too_short (12.2 s)
below min score    0
deduplicated       0
beyond the cap     6
selected          15      max_candidates = 15, so the cap bound
```

Boundary snapping had almost nothing to do: 5 of 15 boundaries moved at all,
mean absolute adjustment 0.06 s, and every start snapped to a segment start.
That is a fact about the prompt format rather than a quality result — the chunk
text presents one timestamped line per segment, so the model returns segment
boundaries because those are the numbers in front of it.

### Preliminary quality, on one video

Eleven intervals were written down from the transcript **before** the call, kept
outside Git. Comparing them to what was selected, by temporal IoU:

- **8 of 11** overlap a selected candidate at IoU > 0.15;
- **2 of 11** reach the IoU 0.50 the specification names for evaluation
  (distributions 0.68, `cp -r` 0.67);
- the hidden-files interval was refused as `too_short` (12.2 s), which was
  predicted in the manual notes and is the rule working;
- the `pwd` and relative-path intervals were proposed and fell out at the cap,
  not for being wrong.

Candidates that look genuinely useful: `cp` refusing a directory without `-r`
(problem → cause → fix), removing a non-empty directory, case sensitivity, and
what a distribution is. Weak ones: several are screen narration ("vamos a venir
acá a este icono") that reads as context-dependent however complete the idea is,
and the `small` transcription garbles technical vocabulary, so a topic or hook
can be wrong where the interval is right.

**What this does and does not establish.** The integration is real: a live
provider, a versioned prompt, structured output, seven calls, four artifacts,
proved reuse. The preliminary signal on this one video is encouraging. General
candidate quality remains **undemonstrated** — one video, eleven intervals I
chose from text without watching it, and a loose threshold on most of the
matches. The specification asks for at least five representative videos, and
that work has not been done.

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
- **`model` is the identifier requested. It is also what the provider reported
  back**, on every one of the eight calls made so far: `modelVersion` came back
  as the bare alias `gemini-3.5-flash-lite`, never a suffixed build. A separate
  `model_resolved` field would therefore duplicate `model`, and none was added.
  One provider, one day; `reported_models` is where a suffixed build would first
  appear.
- **The command reports no token usage.** The provider returns counts and the
  adapter accumulates them, but nothing surfaces them from `analyze`, so the
  seven production calls are unmeasured and their cost is not calculable from
  this repository. Only the opt-in live test prints them.
- `configs/quality.toml` restates the packaged defaults verbatim, so it is a
  no-op overlay. The profiles are not shipped inside the wheel either, so a
  wheel-only installation has no `--config` profile to point at.
- The candidate engine has now seen real model output exactly once, on one
  video. That is enough to show the integration works end to end and to give a
  first signal; it is not a measurement of candidate quality. The specification
  asks for at least five representative videos, and the transcript used here came
  from the `small` model, which garbles technical vocabulary.
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
- **A run that has been previewed can no longer be re-analysed.**
  `READY_FOR_REVIEW -> ANALYZED` is not an allowed transition, so
  `analyze --force` on such a run is refused by the state machine rather than
  stranding previews and decisions built on the old shortlist. Cascading
  invalidation is CE-052; until then the workaround is a new run. ADR-030
  records the trade-off.
- **A preview is not byte-reproducible across FFmpeg builds.** Measured on the
  real run, two `--force` regenerations on the same machine produced
  byte-identical MP4s for all 15 candidates, so x264 is deterministic given the
  same build, source and arguments. What is not guaranteed is the same output
  from a different build or version, and x264 embeds its own build identity. So
  the reuse guarantee is "an unchanged run rewrites nothing", not "two machines
  produce identical previews". Every other artifact in the engine is
  byte-comparable across platforms by contract; the previews are the only ones
  that are not, and the only ones that are disposable.
- **The duration tolerance is 1.0 s and is a stage constant.** The 15 real
  previews drifted at most 0.040 s, so the tolerance has a wide margin over
  what a correct encode actually costs. It is wide enough that a badly wrong
  short clip would still have to be more than a second off to be caught, which
  is the intended trade: the check exists to catch a truncated or empty encode,
  not to measure frame accuracy.
- **`review` is a line-oriented prompt loop, not a player.** It prints the path
  to each preview; opening it is the reviewer's job. There is no key that
  launches a video player, because that would mean shelling out to whatever the
  platform associates with `.mp4`.
- **Piping `review` through a legacy Windows codepage mangles accents.** The
  artifacts are UTF-8 and an interactive console renders Spanish correctly;
  redirecting stdout on a machine whose Python defaults to cp1252 replaces
  unmappable characters. `PYTHONIOENCODING=utf-8` fixes it. The data is never
  affected.
- **On Windows, a preview path longer than 260 characters fails.** FFmpeg uses
  the ANSI file APIs and is bound by `MAX_PATH`; past it, it exits 0 and writes
  nothing. Observed at 268 characters while testing the wheel from a deeply
  nested temporary directory. It is caught rather than inherited -- the adapter
  refuses with "FFmpeg reported success but produced no preview at ...", the
  run becomes FAILED_PREVIEW and no partial artifact is left -- but the fix is
  a shorter workspace path, not a code change. Python itself is unaffected, so
  every other artifact in the same run is written normally, which is why this
  surfaces only at the encode.
- **No human decision has been recorded on the real run.** The 15 previews
  exist and the run is READY_FOR_REVIEW; the decisions are the operator's to
  make, and inventing them would fabricate exactly the measurement CE-053 to
  CE-059 are built to read. Full sessions are exercised only against fixtures.

### Four defects found in review, and what they cost

All four were fixed on the branch after the first review pass, each preceded by
failing tests. They are recorded because three of them were failures of the same
kind: a guarantee that was stated in a docstring and not enforced anywhere.

**A reopened review stranded the run.** `review --force` moves a REVIEWED run
back to READY_FOR_REVIEW before asking anything, and writes no decisions until
the first answer. Quitting immediately therefore left a complete set of
decisions beside a status saying the review was open, and every later session
reported "already reviewed" while the status never moved again. `review` now
settles that case: the decisions have already been proved coherent against this
analysis and shortlist, so the status is the only thing that disagrees and it is
corrected, recording the stage with the same fingerprint the completing session
produced. Recovery is narrow on purpose -- a removed, partial or incoherent set
leaves the run open, the last by refusal.

**Publishing a preview set was not transactional.** Generation always was:
everything is encoded and probed in `.staging`. Publication was not -- stale
previews were unlinked, files were replaced one at a time, then the artifacts
were written, so a failure after the first move left a mixture of two runs with
`index.json` describing neither. The previous set was reusable and became
unverifiable, which is the one outcome a failed `--force` was supposed to be
unable to produce. The whole published set is now moved into `.rollback` first
and restored byte for byte on any failure.

Two attempts at the fix were themselves wrong, and both were caught by the
tests rather than by reading: moving the old set aside outside the guard
reproduced the same defect one step earlier, and inferring what to undo from the
directory contents destroyed previous previews that had not been moved aside
yet. The undo now works from a recorded list of what was moved and what was
placed, because names alone cannot tell them apart when an unchanged shortlist
is regenerated.

**The audio codec was never checked.** `_measure` compared the video codec and
refused a missing audio track, but accepted any codec that existed, so a preview
whose audio was stream-copied or transcoded to mp3, opus, vorbis or PCM passed
verification and was recorded in the index as AAC.

**An over-long rejection detail crashed the session.** The 2000-character cap
lives on the model, so a longer detail raised ValidationError out of the prompt
loop and exited 1 as an unexpected internal error -- after earlier decisions had
been saved, so a reviewer who pasted too much text saw a crash and no sign that
their work had survived. The limit is now named once, shown in the prompt, and a
violation prints the model's own message and asks again.

### A fifth defect: the rollback could destroy the backup it was holding

Found in review after the four above were fixed, and worse than any of them,
because the code that caused it was the fix for defect 2.

Publication moves the published set into `previews/.rollback/` and restores it if
the new set cannot be placed. If a *restore* rename failed, the OSError replaced
the original error on the way out of `_publish` and `generate`'s `finally` ran
`shutil.rmtree(.rollback, ignore_errors=True)` unconditionally — deleting the
only remaining copy and leaving `previews/` empty. Three separate places deleted
the backup and none checked whether the restore had finished: that `finally`,
`_publish` clearing a pre-existing backup before starting, and `_roll_back`'s own
cleanup.

The fix changes what is promised, not just the code. Atomicity is not available
when the undo can fail, so the guarantee is now **durability**: every file of the
previous set stays in `previews/` or in `previews/.rollback/`, the backup is
deleted in exactly two places — after publication placed everything, or after a
restore moved everything back — the error names the directory holding the data,
and the next `preview` run finishes the restore from a journalled phase.
ADR-031 records the reasoning, including why publishing by directory rename was
rejected: on Windows a directory rename fails while a player holds a preview
open, the two-rename swap is not atomic either, and same-filesystem renames are
already guaranteed by keeping the working directories inside `previews/`.

### A sixth defect: resuming a partial restore lost what it had recovered

The two-phase journal was not enough, and the gap was found by review rather
than by the suite.

`placing` means "the previews directory holds files from the failed
publication", and its undo deletes them before moving the previous set back.
Correct the first time; destructive the second. Once two of five files had been
moved back, the directory no longer held only new files — but the journal still
said `placing`, because the two states are indistinguishable from the directory
contents alone. Resuming re-ran the deletion, removed the two recovered files,
and moved back only the three still in the backup. Five files in, three out, and
`verify_previews` failing.

A third phase closes it. `restoring` is written **after the last deletion and
before the first move back**, and its undo never deletes anything. Moving a file
back is idempotent, so a resume simply continues with whatever is left in the
backup; and if the transition write itself fails, nothing has moved, the phase
on disk is still `placing`, and a resume re-runs a deletion that now finds
nothing to delete before retrying.

The test that had covered this went through `generate`, which publishes a fresh
set — complete and verifiable whether or not anything was recovered. It could
not tell recovery from replacement and passed over the defect. Recovery is now
asserted by calling `resolve_pending_rollback` directly.

### SonarCloud: why the gate passes while the UI shows 0.0% coverage

Measured through the SonarCloud API rather than inferred.

**Coverage on new code is 0.0% because Sonar has no coverage data at all.** The
`coverage` and `new_coverage` measures come back empty and `new_lines_to_cover`
is 0. There is no scanner step in `.github/workflows/ci.yml` and no
`sonar-project.properties`: the project uses SonarCloud Automatic Analysis,
which reads the repository and never receives a coverage report. With zero lines
known to be coverable, the ratio is displayed as 0.0%.

**The gate passes because it has no coverage condition.** Its five conditions
are `new_reliability_rating`, `new_security_rating`,
`new_maintainability_rating`, `new_duplicated_lines_density` and
`new_security_hotspots_reviewed`, and `ignoredConditions` is false. Coverage is
neither measured nor gated there. The coverage gate for this project is
`--cov-fail-under=80` in `pyproject.toml`, and the real figure is 99.60%.

Importing coverage would mean adding the scanner with a `SONAR_TOKEN` secret,
which CI deliberately does not have -- "no secrets" is a stated property of the
workflow. That trade is a decision to take explicitly rather than as a side
effect of this pull request.

**The 29 new issues were all MAJOR code smells**, 28 of one rule
(`python:S5778`, a `pytest.raises` block containing more than one call that
could throw) and one of another (`python:S7632`, a malformed suppression
comment). The gate condition is `new_maintainability_rating ≤ A`, which is a
technical-debt ratio rather than an issue count, so 29 smells over ~6700 new
lines still rate A. Both rules were fixed anyway: S5778 was a real precision
problem, since those tests could pass because the wrong call failed, and fixing
`test_preview_rules` also split tests that had conflated a `PreviewRecord`
refusal with a `PreviewIndex` one.

### Two blind spots CI found, and the guards added for them

Both were environment differences rather than logic errors, and each is now
caught locally.

**An import the CI environment does not have.** Three test modules imported
`click.testing.Result`. typer 0.27 no longer depends on click, and the only path
by which click enters `uv.lock` is `faster-whisper`, which arrives with the
`transcription` extra that CI does not install — so it resolved locally and
failed there. `test_suite_portability.py` now reads every test module's imports
with `ast` and checks each against the transitive closure of the main
dependencies and the dev group taken from `uv.lock`, which is what
`uv sync --frozen --group dev` really installs. Not the declared dependencies:
`test_gemini_analyzer` legitimately imports `httpx`, which nothing declares and
which `google-genai` brings.

**A lint pass served from a cache.** `ruff check --fix` was run on the tests
before the new domain modules existed, so ruff could not resolve
`content_engine.domain.review` and sorted it as third-party. Later local runs
reported a pass from the cache because the files had not changed since that
verdict. The whole battery is now re-run with `--no-cache`, `--no-incremental`
and `-p no:cacheprovider` before any claim of green.

## Current priority

V0.5, Human Evaluation (CE-034–CE-039), on `feat/human-evaluation` and pending
review. V0.4 is complete and merged: the foundation as `70fb6ed`, the
deterministic pipeline and `analyze` as `1b15e0f`, the prompt, the Gemini
adapter and structured output as `5570531`.

The immediate next step is not code. The real run is READY_FOR_REVIEW with 15
verified previews, and the measurement the whole subsystem exists for needs a
person to watch them and decide:

```bash
content-engine review 20260906T134657-aprende-linux-ahora-curso-desde--881437
```

That produces the first real editorial data — approval rate, edit rate and
rejection reasons — which is what CE-053 to CE-059 will read and what will say
whether the prompt is any good. One video is still a signal rather than a
measurement; the specification asks for at least five.

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
