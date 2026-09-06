# Content Engine — Architecture Decisions

This document records the decisions already made and the reasoning another AI must preserve. It is an ADR-style summary, not a substitute for the full specifications.

## ADR-001 — Build the core before the platform

**Status:** Accepted

### Context

The original idea included transcription, AI analysis, clipping, subtitles, branding, thumbnails, metadata, multi-platform publishing, n8n, Docker, cloud and eventually Kubernetes. Building all of that before testing clip quality would hide the core risk behind infrastructure.

### Decision

V0 is a CLI-based technical core. V1 adds product infrastructure only if V0 demonstrates value.

### Consequences

- Faster feedback.
- Less infrastructure code.
- Candidate quality can be evaluated before UI work.
- Some components such as authentication and database persistence are intentionally delayed.

---

## ADR-002 — Human-in-the-Loop before autonomous publishing

**Status:** Accepted

### Context

An AI can generate plausible but weak clips. Automatically rendering and publishing every recommendation would automate low-quality output.

### Decision

AI proposes candidates. A human approves, rejects, or edits timestamps before final rendering/publishing.

### Consequences

- Quality remains under creator control.
- Review decisions become evaluation data.
- Candidate Acceptance Rate can be measured.
- Full autonomy is deferred until there is evidence that it is safe/useful.

---

## ADR-003 — AI understands; deterministic code executes

**Status:** Accepted

### Context

LLMs are strong at semantic interpretation but unreliable as command engines and exact arithmetic/state machines.

### Decision

LLM responsibilities are limited to semantic tasks. Pydantic/domain rules validate outputs. Deterministic code handles score totals, timestamps, deduplication, rendering and persistence.

### Consequences

- Easier debugging.
- Lower risk of prompt injection causing execution.
- Reproducibility improves.
- Provider swaps become feasible.

---

## ADR-004 — Modular monolith / simplified hexagonal architecture

**Status:** Accepted

### Context

The system needs clean boundaries but does not need distributed-system operational complexity.

### Decision

Use layers/ports/adapters inside one codebase. Important external capabilities have interfaces such as `TranscriberPort`, `ContentAnalyzerPort`, `RendererPort`.

### Consequences

- Concrete providers can change.
- Domain stays independent from external SDKs.
- Later extraction into services is possible if real load requires it.
- No microservices in V0/V1 by default.

---

## ADR-005 — Filesystem experiments in V0

**Status:** Accepted

### Context

A database is unnecessary for validating the pipeline but experiment reproducibility is essential.

### Decision

Each V0 execution receives a `run_id` and a dedicated run directory containing source metadata, configuration, transcripts, raw/validated candidates, decisions, renders, logs and reports.

### Consequences

- No database setup in V0.
- Easy inspection and debugging.
- Data can later be migrated to PostgreSQL.
- Stage idempotency and hashes must be implemented explicitly.

---

## ADR-006 — faster-whisper as first transcription adapter

**Status:** Accepted for V0 experimentation, not permanent lock-in

### Context

The project needs multilingual technical transcription, timestamps and local processing potential.

### Decision

Implement a `FasterWhisperTranscriber` behind `TranscriberPort`. Keep configuration for model/device/compute type. Benchmark on real videos before declaring a final production model.

### Consequences

- Local inference is possible.
- Hardware-specific tuning can happen later.
- The domain is not coupled to faster-whisper.

---

## ADR-007 — FFmpeg/ffprobe as deterministic media layer

**Status:** Accepted

### Context

Media inspection, audio extraction, trimming, rescaling, crop/blur composition, subtitle burn-in and encoding are deterministic media operations.

### Decision

Use ffprobe for media inspection and FFmpeg for transformations. Call processes with argument arrays rather than shell string concatenation.

### Consequences

- Mature media primitives.
- No unnecessary MoviePy/OpenCV dependency in the core.
- More explicit filter graphs and error handling.

---

## ADR-008 — Candidate score computed in code

**Status:** Accepted

### Context

The LLM may rate qualitative dimensions but arithmetic must be consistent across runs.

### Decision

LLM returns six 0–100 dimension scores. Python computes:

```text
0.25 Hook
+ 0.20 Value
+ 0.20 Context Independence
+ 0.15 Clarity
+ 0.10 Engagement Potential
+ 0.10 Relevance
```

### Consequences

- Comparable scoring.
- Weights can be versioned/experimented without changing prompts.
- The LLM cannot arbitrarily inflate the total.

---

## ADR-009 — Temporal deduplication before ranking output

**Status:** Accepted

### Context

Overlapping transcript chunks can cause the same good moment to be suggested multiple times.

### Decision

Calculate temporal Intersection over Union (IoU). Treat candidates with IoU >= 0.60 as duplicates by default and retain the stronger candidate, unless future experiments justify merge logic.

### Consequences

- Cleaner review set.
- Overlap-based chunking remains possible.

---

## ADR-010 — Boundary snapping

**Status:** Accepted

### Context

LLM timestamps may point inside a sentence even when the semantic segment is correct.

### Decision

After candidate generation, move start/end toward nearby natural speech boundaries using transcript word/segment timestamps, with a configurable window around ±2.5 seconds.

### Consequences

- Better clip beginnings/endings.
- Candidate semantic selection and editorial boundary quality can be evaluated separately.

---

## ADR-011 — Vertical blur default for technical screen content

**Status:** Accepted as initial default

### Context

A hard center crop can remove terminal/code/interface information from 16:9 technical recordings.

### Decision

Provide `vertical_blur` and `vertical_crop`. Default to `vertical_blur` for technical/screen-heavy material in V0.

### Consequences

- Preserves source frame information.
- Smart face/screen-aware reframing is deferred.

---

## ADR-012 — Clean master before platform-specific variants

**Status:** Accepted

### Context

Different social platforms have different rules and visual conventions. A universal watermarked/branded file can create platform-specific problems.

### Decision

Generate a clean master and introduce platform-specific branding/export adaptations later.

### Consequences

- Core renders remain reusable.
- Publishing adapters can conform to current platform requirements.

---

## ADR-013 — No Kubernetes until there is a scaling problem

**Status:** Accepted / Non-negotiable for V0/V1

### Context

The project is initially a personal tool. Kubernetes would add orchestration, networking and deployment complexity without a validated load problem.

### Decision

No Kubernetes in V0 or initial V1. Later adoption requires measured scaling/operational reasons and a new ADR.

---

## ADR-014 — n8n is external workflow orchestration

**Status:** Accepted for future versions

### Context

n8n is useful for integrations, notifications and automation flows but should not own core media processing/business logic.

### Decision

Keep FFmpeg/transcription/AI candidate logic in the application. Add n8n later for external workflow orchestration such as publication notifications, scheduled checks or integration workflows.

---

## ADR-015 — Prompt and model versioning are part of the experiment

**Status:** Accepted

### Context

Candidate quality can change substantially with a prompt or model change.

### Decision

Record prompt version/hash, model/provider, configuration and run source hash. Store raw outputs where useful.

### Consequences

- Experiments can be compared.
- Regressions become diagnosable.
- “The AI got better/worse” becomes measurable rather than anecdotal.

---

## ADR-016 — Packaged configuration and workspace resolution

**Status:** Accepted

### Context

The default configuration lived in `configs/default.toml` and was located by
walking up from `__file__` to a presumed project root. That works from a
repository checkout and nowhere else. Once the package is installed as a wheel,
`Path(__file__).resolve().parents[2]` resolves to `.venv/Lib`, `configs/` was
never included in the distribution, and every command failed. The same
computation also anchored a relative `workspace.root`, so runs would have been
written inside the installation directory.

Two further copies of the same defaults existed implicitly: the values in the
TOML and the `default=` values on the Pydantic models. Nothing kept them in
agreement.

### Decision

`default.toml` ships inside the distribution at `content_engine/resources/` and
is read through `importlib.resources`. The repository copy is deleted; `configs/`
keeps only the `fast` and `quality` overlays. `project_root()` no longer exists.

The Pydantic sections declare types and invariants but carry **no default
values**, so the TOML is the single source of defaults and cannot drift. A test
asserts that every settings field is required, which makes a second copy
impossible rather than merely discouraged.

`workspace.root` is resolved as `CONTENT_ENGINE_WORKSPACE`, then the TOML value,
with a relative value resolved against the current working directory. It is never
resolved against the installation directory.

### Consequences

- Configuration loads from a checkout, from an installed wheel, from any working
  directory, on Windows and Linux.
- A key removed from the TOML fails loudly at load instead of silently falling
  back to a hidden default.
- The workspace follows the invoking shell, so `doctor` and `run` print the
  resolved absolute path to keep that visible.
- Adding a setting means editing the TOML and the model together; the parity test
  enforces it.

---

## ADR-017 — Run identity, logical hashes and stage fingerprints

**Status:** Accepted

### Context

Reproducibility requires knowing what produced an output. The first
implementation conflated several distinct questions into one hash, and computed
it over the full effective configuration, which includes `workspace.root`. Two
machines running the same experiment produced different hashes, so experiments
were not comparable — the exact thing ADR-015 exists to make possible.

Separately, `device = "auto"` resolves to CPU/int8 on one machine and
CUDA/float16 on another. Those two executions do not produce the same transcript.

### Decision

Three distinct identities, deliberately not merged:

```text
run_id                     unique, readable identity of one execution
config_sha256              portable identity of the logical experiment
transcription_fingerprint  real inputs and conditions of one stage
```

`run_id` stays a timestamp, a source slug and a random suffix. It is **not** a
fingerprint and is never used to decide that a previous directory can be reused:
two runs of the same source and configuration are two experiments.

`config_sha256` covers the effective configuration minus the fields that describe
the environment rather than the computation. Today that is exactly
`workspace.root`; `ENVIRONMENT_ONLY_FIELDS` names it explicitly and any future
`workspace` field must be judged individually — a field that affects processing
or artifacts belongs in the hash. `config.effective.json` still records the
complete configuration for diagnosis:

```text
config.effective.json = full configuration, for diagnosis
config_sha256         = logical configuration that defines the experiment
```

`transcription_fingerprint` covers the audio hash, the model and decoding
options, both the requested and the **resolved** device and compute type, and the
transcript schema and normalization rule versions. It is deliberately not
portable: hardware that resolved differently is a different execution and must
not be treated as interchangeable. Hardware is therefore resolved *before* the
reuse decision, not after.

### Run configuration and stage configuration

`transcribe` may be invoked with a `--config` profile other than the one `run`
recorded. That is allowed — it is how a model is compared against another on the
same audio — but it is never silent, and it means a run legitimately holds two
configurations:

```text
run config    configuration the experiment was created with
stage config  configuration a stage actually executed
```

```text
config.effective.json               the run configuration
transcript/config.effective.json    the transcription stage configuration
```

The run configuration is written once, at creation, and is never rewritten after
the fact: it is the record of the experiment that was set up. The stage
configuration is written by the stage that produced the artifacts beside it, and
records what really ran, including the device and compute type that `auto`
resolved to on this machine — something the run configuration cannot know because
it is decided later.

Keeping only the first was the original defect. A run created under `large-v3`
and transcribed under `small` recorded `large-v3` everywhere, so the run
described an experiment that never happened. Keeping only the second would lose
the setup the experiment was designed with.

`manifest.versions.transcription_model` therefore names the model that actually
produced the transcript, the command reports when the two `config_sha256` values
diverge, and `StageRecord` carries both hashes:

```text
fingerprint          decides whether the artifact may be reused
stage_config_sha256  ties the manifest to the readable stage configuration
```

The fingerprint is opaque on purpose — it is a decision, not a description. It is
not, on its own, an adequate record: nobody can reverse a digest to find out what
beam size produced a transcript. The stage configuration is what makes the run
explain itself, and the two are kept consistent by construction, because every
field of the stage configuration that affects the output is also in the
fingerprint payload. A test reconstructs the recorded fingerprint from the stage
configuration artifact, so the pair cannot drift apart unnoticed.

Only `transcription` writes a stage configuration today. CE-047–CE-052 will
generalise the shape — `manifest.stages` is already a map and `StageRecord`
already carries the hash — but no generic stage service exists yet, and inventing
one before a second stage needs it would be guessing.

`manifest.json` carries `schema_version`. A manifest from an unknown schema is
refused rather than guessed at. `stages` is a map so CE-047–CE-052 can add
fingerprints for later stages without changing the domain; today only
`transcription` is populated. `prompt_version` and `prompt_sha256` are `null`
until a run sends a real prompt; CE-026 fills them for a provider run and they
stay `null` for one replayed from a fixture.

### Consequences

- The same logical experiment hashes identically on any machine, verified by
  installing a wheel in a clean environment and comparing against the checkout.
- A transcript is reused only when its recorded fingerprint matches; otherwise it
  is refused with an explanation instead of silently combined with new settings.
- Forcing a rerun is an explicit user action, never an inference.
- Full per-stage fingerprints and downstream invalidation remain V0.7 work, but
  the shape they need already exists.

---

## ADR-018 — Run state machine and failed-run policy

**Status:** Accepted

### Context

`RunStatus` was an enumeration with no rules. Any value could be assigned to any
run: `CREATED` straight to `COMPLETED`, `COMPLETED` back to `CREATED`, a render
failure recorded on a run that had never been inspected. The five `FAILED_*`
members were never written by any code path, so a run that failed inspection was
left as `CREATED` with an empty directory and its identifier was never printed.

### Decision

`domain/run_state.py` declares the transitions explicitly and they are the only
ones accepted:

- forward along `CREATED → INSPECTED → AUDIO_READY → TRANSCRIBED → ANALYZED →
  READY_FOR_REVIEW → REVIEWED → RENDERED → COMPLETED`, one stage at a time;
- a stage may fail from the state that precedes it, or from its own success state
  when it is re-run;
- from a failure state a retry may reach that stage's success state or fail again;
- writing the current state again is a no-op;
- everything else raises `InvalidRunStateError`.

Failures are classified by the stage that actually broke: ffprobe failing or an
absent audio stream is `FAILED_INSPECT`; WAV extraction failing after a valid
inspection is `FAILED_AUDIO`; the transcriber failing or returning output that
cannot be validated is `FAILED_TRANSCRIPTION`.

A failed run is never deleted automatically. The manifest records the stage, the
exception type, the message and the timestamp, and the CLI prints the `run_id`
and the run path.

`manifest.failure` describes why the run is stopped *now*, not everything that
ever went wrong. A successful advance therefore clears it: a run sitting at
`TRANSCRIBED` with a `FAILED_AUDIO` record attached would be a manifest that
contradicts itself, and `resume` (CE-049) would have to guess which half to
believe. The history of attempts is not kept in the manifest; per-run logs
(CE-062) are where that belongs. A retry that fails again overwrites the record
with the new failure.

### Consequences

- A manifest cannot claim a run reached a state it did not earn.
- The `FAILED_*` states are reachable, tested and meaningful.
- A failure leaves enough on disk to diagnose it.
- `ANALYZED` onwards are declared but unreachable until V0.4 and V0.6; the table
  is written once and covers them.
- `resume` (CE-049) will read this state rather than inventing its own.

---

# Technical contracts

## Run artifacts on disk

Not an ADR, but binding on every artifact the engine writes under `workspace/`:

- UTF-8, no BOM;
- LF line endings, written explicitly so Windows does not translate them;
- written atomically through a temporary file and an atomic replace — every
  artifact, not only the JSON ones, so `transcript.srt` and `transcript.txt` are
  never observed half-written; a failed write leaves neither a partial file at
  the final path nor a `.tmp` beside it;
- JSON never serialized with a `default=` coercion that would hide an
  unserializable value;
- versioned with a `schema_version` where the artifact is read back later;
- free of `NaN`, `Infinity` and `-Infinity`. They are Python extensions no
  conforming JSON parser accepts, and none of them describes a duration, a
  position in audio, a probability or a ratio. Every domain model refuses them
  at the boundary through `allow_inf_nan=False`, provider output is checked with
  `math.isfinite` before any comparison touches it — every ordering test against
  NaN is false, so an unchecked NaN would be silently clamped to whatever bound
  it was compared with — and `write_json` refuses them again as a last defence.

The point is that a run produced on Windows and the same run produced on Ubuntu
are byte-comparable. This contract governs generated artifacts only — repository
text files are normalized by Git.

---

## ADR-019 — Gemini as the initial analysis provider

**Status:** Accepted for V0 experimentation, not permanent lock-in

### Context

ADR-003 and ADR-008 fix what the LLM is allowed to do: interpret semantics and
rate six dimensions. They say nothing about whose model does it. The earlier
documents named OpenAI as the first adapter, chosen before any candidate had
been generated, and `analysis.provider` had exactly one member.

V0.4 needs a provider that is available now, returns structured output against a
declared schema rather than free text a parser has to guess at, and can absorb
the volume of a candidate-quality experiment without the cost of the experiment
becoming the reason not to run it. Candidate quality is the project's main risk;
anything that discourages iterating on prompts works against measuring it.

### Decision

Gemini is the initial analysis provider for V0.

```text
provider     gemini
model        gemini-3.5-flash-lite
SDK          google-genai
adapter      GeminiContentAnalyzer
credential   GEMINI_API_KEY
```

The OpenAI compatibility layer Gemini offers is **not** used. It would put a
translation shim between the domain and the provider whose failure modes belong
to neither, and it would make the adapter pretend to be something it is not. The
native SDK is used directly, behind the port.

`ContentAnalyzerPort` stays provider-neutral. It is a Protocol over domain types
only, and nothing above the adapter boundary may import `google.genai` or name
Gemini. Adding OpenAI later is a second adapter and a configuration value, not a
change to the domain.

faster-whisper remains the transcription system. This decision concerns the
analysis stage alone.

### Consequences

- The domain does not depend on Google. `ContentAnalyzerPort` is written against
  `TranscriptChunk` and `RawCandidate`, and the SDK types stay inside
  `adapters/analysis/`, the same boundary ADR-006 draws around faster-whisper.
- Changing provider changes **nothing** about scoring, timestamp validation,
  boundary snapping, deduplication or ranking. Those are deterministic code by
  ADR-003 and ADR-008, they consume `CandidateScores` and intervals, and they
  cannot tell which model produced them. That is the property that makes a
  provider swap an experiment rather than a rewrite.
- Reproducibility is not a promise that a given call returns identical bytes. No
  generation parameters are pinned in V0: default sampling is kept, so two calls
  with the same prompt may differ. What is reproducible is everything around the
  call — the exact model, the prompt version and hash, the effective
  configuration, the input transcript hash, the raw responses stored per chunk,
  and every deterministic rule applied afterwards. An experiment is comparable
  because its inputs and its rules are recorded, not because the model is
  assumed to be a pure function.
- `GEMINI_API_KEY` is read from the environment and nowhere else. It never
  reaches `manifest.json`, `config.effective.json`, any stage configuration, any
  artifact, any log line or the repository. `doctor` reports only whether it is
  present, never its value, and `.env` stays ignored while `.env.example` carries
  the name with an empty value.
- The free tier is a shared-quota service. Do not send confidential or sensitive
  recordings through it. This is a policy about which material may be analysed,
  not a claim about the provider.
- Nothing here is implemented yet. This ADR records the decision so the port,
  the configuration and the schemas can be built against it; the adapter, the
  SDK dependency and the prompt arrive in a later pull request.

---

## ADR-020 — Candidate records describe the phase they reached

**Status:** Accepted

### Context

The first shape of the candidate models had a single `ValidatedCandidate` with a
required interval, a required `BoundaryAdjustment` and a required total score,
and a `RawCandidate` whose Pydantic constraints refused a negative start or a
non-positive end.

Both were wrong in the same direction. A proposal refused by CE-030 for having
an inverted interval never gets a boundary or a score, so the only way to record
it in that model is to invent values for fields it never earned. And the raw
model refused exactly the timestamps CE-030 exists to reject, so an impossible
proposal became a parse error with nothing written down — replacing a
measurement of how often the prompt fails with an exception.

The pipeline has phases, and a record has to be able to say which one it reached.

### Decision

**Untrusted output is preserved, not policed.** `RawCandidate.start` and `.end`
carry no ordering or sign constraint. Negative, zero-length and inverted
intervals are accepted and kept unaltered. `NaN` and the infinities remain
refused: an impossible timestamp is data about the prompt, a non-number is not a
timestamp at all.

"Verbatim" belongs to one field only: `CandidateBatch.raw_response`, which the
port keeps exactly as the provider sent it, so a parse failure still leaves
evidence. `InvalidCandidate.proposed` is the *parsed* proposal, preserved
without alteration but structured — the distinction matters when the disagreement
under investigation is between what the provider said and what the domain made
of it.

**Two record types, split by phase reached**, rather than one type with optional
fields:

```text
InvalidCandidate     refused by CE-030, before snapping or scoring.
                     Holds the parsed proposal and the reasons. No interval, no
                     boundary, no deterministic total - it never earned them.
                     `proposed.scores` still carries the six ratings the
                     provider supplied: those arrived with the proposal rather
                     than being computed from it.

ValidatedCandidate   reached scoring. Interval, boundary and total are all real.
                     Status is SUGGESTED, REJECTED or DEDUPLICATED.
```

Optional fields were the alternative and were rejected: they would let a caller
ask a question that has no answer, and every consumer in CE-030–CE-033 would
carry a `if x is not None` that the type system could not check. Two types make
the phase a fact the compiler knows.

`CandidateCollection` therefore holds three lists — `candidates`, `rejected`,
`invalid` — split by how far a proposal got rather than by how good it was.

**`BoundaryAdjustment` may be absent, but only by being on the other type.** It
exists exactly when snapping ran, which is exactly when the record is a
`ValidatedCandidate`. It is never optional within a type.

**A reason belongs to the phase that could have decided it.** `enums.py` names
the sets: `PRE_SCORING_REASONS` for what CE-030 can reach, and one named
constant each for `BELOW_SCORE_REASON`, `DEDUPE_REASON` and `TOP_N_REASON`.
`TERMINAL_REASONS` maps a status to the reasons it may carry.

```text
InvalidCandidate     one or more PRE_SCORING_REASONS. Several are allowed: a
                     single CE-030 pass can find more than one defect.
SUGGESTED            no reasons at all.
REJECTED             exactly one, BELOW_MIN_SCORE or NOT_IN_TOP_N.
DEDUPLICATED         exactly [DUPLICATE], and never a rank.
```

Reasons from different phases cannot be mixed. Without this a duplicate could be
filed as a plain rejection and counted as a score failure, and every total in the
funnel would still add up.

**`CandidateCounts` are terminal outcomes, mutually exclusive by construction.**
Every proposal reaches exactly one of `invalid`, `below_min_score`,
`deduplicated`, `not_in_top_n` or `selected`, so they sum to `proposed` and the
model enforces it. `not_in_top_n` was added because a candidate that survived
every rule and was still cut by `max_candidates` is a real outcome that
previously had nowhere to go, which would have made the identity false. The
exclusivity comes from the pipeline order: the minimum-score filter runs before
deduplication, and the top-N cut runs last.

**Every counter is read off the records, not merely required to balance.** Each
of the five is counted from the list that holds it. The weaker check —
`below_min_score + deduplicated + not_in_top_n == len(rejected)` — accepts any
permutation of those three, and the difference between "the prompt scores badly"
and "the cap is too tight" is exactly what the funnel exists to report.
`counts.deduplicated == len(deduplication_events)` needs no separate check: the
event rules below already make the two the same number.

**A deduplication event is evidence, so it is held to the records it names.** It
restates facts that live on both candidates, which is what makes it an audit
trail and also what lets it disagree with them. For each event:

```text
kept_id != dropped_id             a candidate cannot deduplicate itself
dropped_id                        is recorded DEDUPLICATED, and dropped once
every DEDUPLICATED candidate      appears as dropped_id exactly once
kept_id                           survived deduplication: never DEDUPLICATED,
                                  never BELOW_MIN_SCORE
kept_score, dropped_score         equal the totals on the two records
kept_score >= dropped_score       deduplication keeps the better one
iou                               recomputed from both intervals, and >= dedupe_iou
```

The pipeline order decides who may appear on which side. The minimum-score
filter runs first, so a keeper was never removed by it; the top-N cut runs last,
so a keeper may still end up `NOT_IN_TOP_N` rather than `SUGGESTED`.

**Equal scores are broken deterministically**: the earlier `start` is kept, and
if the starts are equal too, the smaller identifier. A rule is needed because
without one the same input can produce two different shortlists, and the
identifier is a stable last resort because it is derived from the proposed
interval before snapping (D-3).

Float comparisons in all of this use an explicit tolerance: a microsecond for
timestamps, `1e-6` for totals and for the overlap ratio. These are binary
floats, so equality between a value and the arithmetic that produced it is only
ever equality to within representation error.

### Consequences

- CE-030–CE-033 can be written against invariants the models enforce rather than
  against convention: the interval matches the boundary, the deltas describe the
  movement, the counts match the lists, identifiers are unique, and a
  deduplication event cannot name a candidate that does not exist.
- A refused proposal is still fully diagnosable, which is what makes the failure
  modes in the candidate engine specification measurable rather than anecdotal.
- Adding a sixth terminal outcome later means adding a counter and updating the
  identity, which will fail loudly rather than silently unbalancing the funnel.

---

## ADR-021 — `target_candidates` is a run objective, never a per-chunk quota

**Status:** Accepted

### Context

`CandidateCollection.target_candidates` documented the objective for the whole
run, while `AnalysisContext.target_candidates` — passed into a call about a
single chunk — documented it as the objective per chunk. Same name, two
meanings, on both sides of the port. A ten-chunk recording would have produced
either ten candidates or a hundred depending on which reading an adapter
believed.

### Decision

There is one semantics. `target_candidates` is the objective for the whole run.
It is not a number of candidates requested from each chunk, and `max_candidates`
remains the hard ceiling CE-033 applies once, over every chunk's output together.

The field on `AnalysisContext` is renamed `run_target_candidates`, because the
ambiguity was created by the name: a field called `target_candidates` on a
per-chunk call reads as "return this many for this chunk" no matter what the
docstring says.

If an adapter ever needs a per-chunk budget, it is a separate field with its own
name, computed explicitly from the run objective and the chunk count. It is never
this field reinterpreted.

### Consequences

- An adapter cannot silently multiply the run objective by the chunk count.
- The two sides of the port can be read independently without the reader having
  to hold the distinction in their head.

---

## ADR-022 — The deterministic pipeline's binding rules

**Status:** Accepted

### Context

CE-030 to CE-033 were specified as intentions rather than as algorithms. The
specification asks for candidates "grounded near transcript content" without
saying what near means, for boundaries snapped to "nearby" edges without saying
which one wins when two are equally near, and for duplicates to keep "the
stronger candidate" without saying what happens when two are equally strong.

Every one of those gaps is a place where two correct-looking implementations
produce two different shortlists from the same transcript. That is fatal to the
only thing V0.4 exists to measure: whether a change to the prompt changed the
candidates. If the deterministic half is not deterministic, a difference cannot
be attributed to anything.

### Decision

**One pipeline order, and it is binding.**

```text
proposals from every chunk
  -> CE-030 validation           invalid
  -> CE-031 boundary snapping    only for intervals that survived
  -> CE-025 deterministic score
  -> minimum-score filter        below_min_score
  -> CE-032 global deduplication deduplicated
  -> CE-033 global ranking
  -> max_candidates ceiling      not_in_top_n
  -> selected
```

The order is what makes the five outcomes of ADR-020 mutually exclusive. The
score filter runs before deduplication, so a candidate below the threshold can
never be the reason a good one disappears. The ceiling runs last, over every
chunk's output together.

**Grounding is arithmetic, and its tolerance is the snapping window.** An
instant is grounded when it falls inside a segment or word interval of its
chunk, or lies within `boundary_snap_seconds` of a real segment or word edge.
Both endpoints must be grounded. No text similarity, no heuristic, no model.

Tying the tolerance to the snapping window is the substantive choice: an
endpoint CE-031 could not have reached a boundary from is an endpoint nothing in
the transcript supports, and accepting it would mean clipping audio the analyzer
never saw a boundary for. A consequence worth stating: because a word is
contained in its segment, word edges can never ground an instant that segment
edges do not. They are consulted because the rule is defined over both, not
because they decide anything today.

**CE-030 accumulates every applicable reason** in a fixed order, rather than
stopping at the first. A proposal that is both inverted and outside its chunk
says something different about the prompt than one that is merely inverted, and
the whole point of keeping refused proposals is to be able to see that. Duration
rules are skipped when there is no positive duration to judge: calling an
inverted interval "too short" would invent a measurement of something that is
not a length.

**Snapping has a total order.** Nearest edge wins; a segment edge beats a word
edge at the same distance; a remaining tie takes the earliest for a start and
the latest for an end. The last rule widens the clip rather than narrowing it,
because a clip that begins a word early is watchable and one that begins a word
late is not.

**An adjustment that leaves the interval unusable is reverted whole** — inverted,
past the end of the source, or outside the duration policy — restoring the
proposal with zero deltas and unchanged anchors. Reverting rather than
discarding is the rule, and half an adjustment is never kept: ADR-010 asks for
better boundaries, not for fewer candidates, and the analyzer's judgement that a
moment is worth clipping outranks an editorial refinement.

**Deduplication is greedy over one priority order** — total descending, then the
earlier start, then the identifier — applied globally across chunks, comparing
each candidate against the survivors in that same order. This makes the keeper
well defined when a candidate overlaps several survivors, and it does not
require the duplicate relation to be transitive, which it is not: A can absorb B
while C, which overlaps B but not A, survives as the different moment it is.

**CE-033 reuses that identical order** for ranking. A total exactly at
`min_score` survives; only a total below it is refused.

**A candidate's identifier is computed before snapping**, from the transcript
digest, the chunk, the ordinal of the proposal within its batch, the proposal
itself and the prompt identity. Before, because the identifier is the last
tie-break in an ordering that runs before snapping has happened. Including the
chunk and the ordinal, because deriving it from content alone would give the
same identifier to the same moment returned by two overlapping chunks — and that
pair is exactly the record the funnel needs in order to count a duplicate.

### Consequences

- The same proposals produce the same collection, in the same order, on any
  machine, and permuting the batches changes nothing.
- Every one of these rules is versioned in the stage configuration, so a change
  to snapping or deduplication invalidates reuse even though no setting moved.
- Grounding is coarser than a semantic check would be. A timestamp inside a long
  monologue is grounded wherever it falls. That is accepted: this rule exists to
  refuse timestamps the transcript cannot support at all, not to judge whether
  the moment is a good one.

---

## ADR-023 — A fixture analyzer is the executor until the provider exists

**Status:** Accepted

### Context

The deterministic pipeline, its four artifacts and its reuse rules have to be
built and exercised before the Gemini adapter of ADR-019 exists. Running them
needs an analyzer. Using the real one would put a paid, non-reproducible network
call inside every test of code that is meant to be deterministic, and would make
CI depend on a credential.

### Decision

`FixtureAnalyzer` implements `ContentAnalyzerPort` by replaying answers recorded
in a strict, versioned JSON file. It is temporary infrastructure for PR B and its
tests, never a production provider.

**It names itself.** The analyzer is `fixture`, its model is the fixture's, and
its prompt identity is `fake-fixture/v1`, hashed from a versioned template.
`manifest.versions.analysis_provider` and `analysis_model` are updated to what
actually ran, exactly as the transcription stage records the model that really
produced a transcript. A run analysed from a file must never leave behind a
manifest asserting a Gemini call that never happened.

**It does not borrow CE-026's identity.** `manifest.versions.prompt_version`
and `prompt_sha256` stay null for a replayed run even now that
`clip_candidates/v1` exists, because a fixture did not send it. The fake's own
identity goes in `analysis/config.effective.json`, where the field means "the
prompt identity of whatever ran" and `fake-fixture/v1` is true.

**`AnalysisProvider` is not touched.** It still has one member, `gemini`, and the
packaged defaults still name it. `fixture` is a run-time fact recorded in
artifacts, not a configurable provider.

**`--fixture` is required.** A command that silently produced nothing without one
would be dishonest about which half of the stage exists.

### Consequences

- The whole analysis stage runs in CI with no network, no SDK, no credential and
  no cost, and every test of it is deterministic.
- A fixture batch carries candidates or a failure, never both. It may keep the
  raw response beside an error: what the provider said before it was judged a
  failure is the most useful thing about the failure.
- Adding the Gemini adapter changes which object is constructed in `cli.py` and
  nothing else: the service, the pipeline and the artifacts are provider-neutral
  by construction, and `AnalyzerIdentity` already carries the shape a real
  provider needs.
- Artifacts produced by a fixture run are marked as such and cannot be confused
  with provider output when the comparison eventually matters.

---

## ADR-024 — The analysis fingerprint covers the artifacts, not only the inputs

**Status:** Accepted

**Supersedes** the reuse contract in ADR-022 as first implemented.

### Context

ADR-017 defines a stage fingerprint as the identity of what a stage consumed.
That works for transcription, which writes one artifact the reuse check reads
back and validates in full: an edited `transcript.json` is caught by the reader,
so the fingerprint only has to answer "were these the same inputs".

Analysis writes four artifacts. The first implementation followed the
transcription shape — a digest over the transcript, the raw batches and the
stage configuration — and an independent review found the gap that leaves.
`chunks.json` was never read back at all, on the argument that it could be
rebuilt from the transcript. `candidates.json`, the shortlist a human is
actually shown, was read only to check its own invariants. The identity fields
above the raw batches were not covered either.

So a candidate's topic, its interval, its score, the order of the ranking, the
text of a chunk or the recorded analyzer version could all be edited between two
runs, every artifact would still validate, and the second run would reuse them
and report success.

### Decision

The analysis fingerprint covers the transcript digest and the **whole** of all
four artifacts, each serialized canonically. Whole models, not chosen fields:
selecting a subset is precisely how the first version left two artifacts
unprotected, and selecting correctly would require knowing in advance which
edits matter.

`ANALYSIS_FINGERPRINT_VERSION` is 2. Artifacts written under version 1 are
refused rather than reused under a weaker digest.

This makes it an **integrity digest over one execution** rather than a portable
identity. Two runs of identical inputs produce different fingerprints, because
`generated_at` lives inside `candidates.json`. That is accepted: the question
this digest answers is "may these four files be reused", not "is this the same
experiment". `config_sha256` and `stage_config_sha256` remain the portable
identities, and neither changes.

**A digest is not enough on its own**, so `coherence_problem()` checks the four
against each other and against the current transcript. A set of artifacts copied
from another run would be internally consistent, would rebuild its own
fingerprint, and would be entirely wrong about the run holding it. The same
check runs before anything is written, so an incoherent set cannot be produced
either.

**All four are read back and validated**, including `chunks.json`, which gets
the strict versioned reader it never had, and is compared against the chunks the
current transcript and settings produce. A file trusted because it could be
regenerated is a file nobody checked.

### Consequences

- Editing any field of any analysis artifact is refused with exit code 3, and
  the refusal writes nothing.
- Reuse costs four file reads and four model validations instead of two. That is
  paid once per invocation and is far below the cost of the stage it skips.
- The analysis stage and the transcription stage now compute their fingerprints
  differently. That is deliberate and the reason is written down here, rather
  than left as an inconsistency for a later reader to discover.
- When CE-047–CE-052 generalise stages, this is the shape a multi-artifact stage
  needs; transcription can keep the simpler one because it writes one artifact.

---

## ADR-025 — The prompt is a packaged resource, not a repository file

**Status:** Accepted

### Context

`V0_IMPLEMENTATION_SPEC.md` places the candidate prompt at
`prompts/clip_candidates/v1.txt`, at the repository root. That reads naturally
in a source tree and does not survive installation: `pyproject.toml` builds
`src/content_engine` into the wheel and nothing else, so an installed
`content-engine analyze` would look for a prompt that is not there.

The prompt is also hashed. Its SHA-256 is recorded in `manifest.json` and in
`analysis/config.effective.json` for every run that used it, and ADR-015 makes
that hash part of the identity of an experiment.

### Decision

The prompt ships inside the package:

```text
src/content_engine/resources/prompts/clip_candidates/v1.txt
```

It is read through `importlib.resources`, traversed from the
`content_engine.resources` package rather than addressed as a package of its
own — `prompts/clip_candidates/` contains no Python, and asking `files()` for a
namespace package is the kind of thing that works in a checkout and fails in a
zipped wheel. Both the wheel and the sdist were checked for the file.

The digest is taken over the text with line endings normalised to LF, and
`.gitattributes` pins `*.txt` to LF as well.

`prompts/README.md` stays at the root and now explains where prompts actually
live, so the path in the specification resolves to an explanation rather than to
nothing.

### Consequences

- The prompt is found from a checkout, an installed wheel, any working
  directory, and paths containing spaces and non-ASCII characters.
- The same prompt has the same identity on Windows and on Linux. Hashing raw
  bytes would have given a CRLF checkout a different digest, and every manifest
  written on a developer machine would have looked like a different experiment
  from the identical run in CI. The normalisation makes that hold even where a
  checkout is configured differently; the `.gitattributes` rule means it does
  not have to.
- Editing a prompt in place changes the identity of the experiment. New prompt
  versions are new files, not edits.
- The specification's illustrative path is now wrong, and this ADR is the
  record of why.

---

## ADR-026 — `google-genai` is a runtime dependency, not an optional extra

**Status:** Accepted

### Context

`faster-whisper` is an optional extra (`uv sync --extra transcription`), and the
adapter imports it lazily with a clear message when it is absent. That is the
established pattern in this repository, and the obvious thing to copy.

`V0_IMPLEMENTATION_SPEC.md` lists `google-genai` among runtime dependencies.

The two point in opposite directions, so the choice has to be made rather than
inherited.

### Decision

`google-genai` is a required runtime dependency, pinned as `>=2.22,<3` and
locked in `uv.lock`.

The lazy import stays. `load_sdk()` still catches `ImportError` and raises a
`ConfigurationError` naming the package, because a required dependency can still
be missing from a broken or partial installation, and that should exit 2 with a
sentence rather than a traceback.

### Consequences

- The divergence from the `faster-whisper` precedent is about weight, not about
  consistency for its own sake. `faster-whisper` pulls CTranslate2, whose wheels
  are large, platform-specific and bound up with a CPU-or-GPU decision the user
  has to make; making that optional spares everyone who does not transcribe
  locally. `google-genai` is a thin HTTP client over `httpx`, `google-auth` and
  `pydantic`, and the last of those is already a hard dependency.
- `analysis.provider` defaults to `gemini`, so the default configuration can be
  executed by a default installation. An extra would have made the
  out-of-the-box `analyze` fail on a missing package rather than on a missing
  key, which is a worse first experience and a less accurate diagnosis.
- CI installs the SDK, so the adapter's request construction is exercised
  against the real SDK types rather than against a stand-in for them. A
  misspelled parameter fails in CI instead of on the first paid call.
- The SDK being installed is never the same thing as the provider being
  reachable. No test in the normal suite opens a socket, and the credential is
  read only when a real analyzer is constructed.

---

## ADR-027 — Retries, and what a Gemini failure means

**Status:** Accepted

### Context

Two things had to be decided together: which failures are worth another call,
and which exit code each failure produces. Getting the second wrong is worse
than getting the first wrong — a run marked `FAILED_ANALYSIS` because a laptop
has no API key records a provider failure that never happened.

### Decision

**Exit 2, configuration, and the run is not touched:** `GEMINI_API_KEY` absent
or blank; the SDK not importable; `analysis.model` empty or the placeholder; a
provider named in the configuration that this build has no adapter for. Nothing
was called, so nothing is recorded.

**Exit 3, incompatible artifact:** the existing candidates do not match what is
being asked for now — including a switch between fixture and provider, which
falls out of the stage configuration digest without a special case.

**Exit 5, analysis, and the run becomes `FAILED_ANALYSIS`:** a timeout, an
exhausted rate limit, a 5xx, a refusal, an empty response, a truncated or
schema-invalid answer, and a reply verifiably produced by a different model. The
stage started and could not finish.

**Retried:** transport errors and timeouts, 408, 429, 500, 502, 503, 504. Three
attempts, fixed backoff of 2s then 8s. The worst case per chunk is ten seconds
of waiting, which is a number rather than a property of an unbounded exponential.

**Not retried:** 400, 401, 403, 404, and any schema violation. These fail
identically every time; a retry spends quota to obtain the same answer.

An answer is refused when the reported `model_version` is neither the model
requested nor a dated build of it (`gemini-3.5-flash-lite-001` is the same
model). When nothing is reported there is nothing to verify, and an unverifiable
claim is not evidence of a mismatch.

### Consequences

- Error messages are rebuilt from the status code and a bounded, redacted
  excerpt of the provider's message. The SDK's own `str()` embeds the entire
  decoded response body: unbounded, provider-controlled, and exactly where an
  echoed key would appear. The environment is read on the failure path only, in
  order to delete the credential from the text — the one use of the variable
  that cannot leak it.
- **A response that fails to parse is not persisted.** `raw_response` is only
  carried on a `CandidateBatch`, batches only exist on the success path, and
  nothing is written until all four artifacts are valid. So the single most
  useful artifact for debugging a bad prompt — the malformed reply — is lost,
  and only a bounded excerpt survives in the error message. This is a real
  limitation of the current contract, recorded rather than worked around:
  writing a partial artifact to keep it would break the guarantee that a failed
  stage leaves nothing behind, which is worth more.
- Retry counts and backoff are module constants, not configuration. They change
  how long a failure takes, never what the artifacts contain, so they are
  deliberately outside the stage configuration and the fingerprint.
- **`model` is the identifier that was requested, and — as observed — also the
  one reported back.** The response carries `modelVersion`, documented only as
  "the model version used to generate the response", and the published reference
  does not say whether it returns the alias or a dated build. It has now been
  measured rather than guessed: across eight real calls on 2026-09-06,
  `modelVersion` came back as **`gemini-3.5-flash-lite`** every time — the alias
  that was requested, with no revision suffix. The adapter accepts the requested
  model or the requested model followed by `-`, refuses anything else, and
  records the requested identifier in every artifact.

  So on this evidence a separate `model_resolved` field would hold a copy of
  `model` and nothing more, and none was added. This is one provider on one day;
  should a future run report a suffixed build, the field becomes justified and
  `reported_models` is where that will first show up.

  No schema was widened to hold a resolved model. The reported
  values are collected on the analyzer as `reported_models` and surfaced in
  exactly one place: the diagnostics printed by the opt-in live test in
  `tests/ai/`. They do **not** reach `analyze`'s output, the manifest or any
  artifact, and this consequence previously implied an "end-of-run report" that
  does not exist. Surfacing a value nobody has ever observed would be plumbing
  built on a guess; the opt-in test is where a first observation will appear.

  If a live run shows the provider naming a specific revision, adding
  `model_resolved` beside `model` -- and reporting it from the command -- is a
  schema bump with evidence behind it. Until then a run records the model it
  asked for, and this says so plainly rather than letting "the exact model" be
  assumed.

---

## ADR-028 — Preview encoding is a stage constant, not an experiment variable

**Status:** Accepted

### Context

CE-034 needs a preview to be described precisely enough that a later run can
decide whether to re-encode: dimensions, codecs, encoder preset, CRF, sample
rate, channels, and how the source frame is fitted into the vertical one. Every
other stage puts its settings in the configuration file, and `[render]` already
holds exactly this list of keys for the final output.

The obvious move was to do the same for `[preview]`. It has a cost that is easy
to miss: `config_sha256` is computed over the whole configuration and is the
logical identity of an experiment. Adding six encoder keys to `[preview]`
changes that digest for every run, past and future — so the identity of an
experiment about prompts and candidate quality would move because a throwaway
proxy's CRF is now written down.

### Decision

`[preview]` keeps the three keys it already had: `enabled`, `width`, `height`.
They are the ones an operator plausibly changes and the only ones that alter
what a reviewer sees.

Everything else — `libx264`, `veryfast`, CRF 30, `yuv420p`, AAC at 96k, 44.1 kHz
stereo, `fit_pad` with black padding — is a constant of the preview stage in
`domain.preview_rules`, versioned by `PREVIEW_RULES_VERSION` and
`PREVIEW_ARGUMENT_VERSION`, and written out in full to
`previews/config.effective.json`.

The two versions are separate deliberately. The policy can stay the same while
the argument list that expresses it changes, and it is the argument list that
decides the bytes. Either one moving changes the stage configuration digest and
therefore requires an explicit `--force`.

### Consequences

- A preview stage still explains itself completely: the effective configuration
  artifact names every parameter that produced the file beside it, so nobody has
  to read the source to find out what CRF a preview was encoded at.
- Changing an encoder parameter is a code change with a version bump, not a
  profile edit. That is the right friction for a proxy: it is not something to
  sweep across an experiment matrix.
- `config_sha256` for the "Aprende Linux" run analysed before this branch is
  unchanged, so V0.5 did not invalidate the identity of a real experiment in
  order to describe files that are deleted at the end of it.
- If preview encoding ever becomes worth experimenting with, the keys move into
  `[preview]` with a `config_sha256` bump and this ADR is superseded. Nothing
  here makes that harder — the parameters already flow through one readable
  model.
- The verification target is stated separately from the encoder name. `libx264`
  produces a stream ffprobe calls `h264`, so `video_codec` and
  `expected_video_codec` are two fields; collapsing them would make the
  post-encode check compare a name against a different name and fail on every
  correct preview.

---

## ADR-029 — Human decisions are three types, and a rejection has no interval

**Status:** Accepted

### Context

`V0_IMPLEMENTATION_SPEC.md` section 27 sketches one model for a review
decision: `candidate_id`, a `decision` literal, the original bounds, then
`final_start`, `final_end` and `reason` all optional.

Four optional fields and a literal tag. Written that way, the type permits a
rejection carrying an approved interval, an approval carrying a rejection
reason, an "edit" whose final bounds equal the original ones, and an approval
with no final bounds at all. Each of those is a record that says something
different from what the person did, and this file is the only evidence in the
whole engine of what a person decided.

The PR brief also lists `final_start` and `final_end` among the fields *every*
decision must keep. For a rejection there is nothing to keep: no interval was
approved. That is a real contradiction in the specification, and it is resolved
here rather than silently.

### Decision

Three models, discriminated on `decision`, in `domain/review.py`.

`ApprovedDecision` carries `final_start` and `final_end`, validated to equal the
original interval. They are stored rather than derived so a renderer can read
`final_interval` without knowing which kind of decision it holds, and checked
rather than trusted so a file claiming an approval with moved bounds is refused
instead of being read as an edit.

`RejectedDecision` carries an optional `EditorialReason` and an optional
`detail`, **and no final bounds at all** — absent, not null. Storing the
original values there would produce a row that reads as an approved clip, which
is exactly the confusion CE-057's rejection metrics exist to measure. Anything
that needs "the interval to render" asks `final_interval`, which answers `None`.

`EditedDecision` carries final bounds that must be finite, ordered, inside the
source, and actually different from the original ones. An edit that moved
nothing is an approval, and is refused as an edit.

Beyond that:

- **`EditorialReason` is a separate enum from `RejectionReason`.** The latter
  records why the deterministic rules discarded a proposal —
  `invalid_interval`, `below_min_score`, `duplicate` — decisions made before any
  person saw anything. The former records an editorial judgement about material
  that passed every rule. Three of the names collide (`too_long`, `too_short`,
  `duplicate`), and sharing one enum would make a single value mean "under
  `min_duration_seconds`" in one row and "not worth posting" in the next.
  CE-057 could then no longer separate the prompt's failures from the operator's
  taste, which is the measurement this project exists to make.
- **A reason is optional; `other` requires a detail.** Making a reason mandatory
  would push a reviewer working through thirty candidates towards whichever
  label was least effort, and a guessed label is worse than a missing one for
  the metrics reading it. `other` is the exception because it names nothing: the
  free text is its whole content.
- **A detail without a reason is refused.** The detail explains a reason rather
  than replacing it; allowing a bare detail would create a second, unstructured
  reason channel that no metric can group by.
- **A human edit is not constrained by `min_duration_seconds` or
  `max_duration_seconds`.** Those shape what the model may propose, and CE-030
  enforces them on proposals. A person watching the preview is the authority on
  where their clip ends, and silently widening their interval back to the AI
  minimum would render something they did not choose. The only bounds an edit
  respects are finite, ordered and inside the source.
- **`reviewed_at` must be timezone-aware UTC.** A naive timestamp is unorderable
  against an aware one, and a local one reorders the session when the file is
  read on another machine.

### Consequences

- Reading a decision means knowing which kind it is. That is the point: a
  consumer cannot accidentally treat a rejection as a clip to render, because
  the fields it would need are not there to read.
- `review/decisions.json` is not a flat table. A consumer that wants one can
  project it; the stored form does not pretend the three kinds are one thing.
- The collection checks each decision's copied original bounds against the
  candidate. Without that, an edited original would turn "the reviewer moved the
  boundary by four seconds" into a measurement of nothing, and that difference
  is precisely what CE-057 reads.
- Adding a fourth kind of decision means adding a model, not adding another
  optional field to a growing one.

---

## ADR-030 — Re-opening a review is the only backwards transition

**Status:** Accepted, extends ADR-018

### Context

ADR-018 makes the run state machine strictly forward: a run advances one stage
at a time or stops at the failure state of the stage that broke, and backwards
moves are refused so a manifest can never claim a state it did not earn.

`review --force` breaks that assumption for one specific reason. It discards
human decisions and asks about every candidate again. A run in `REVIEWED` whose
`decisions.json` has just been replaced with an empty collection would be
claiming a finished editorial pass while the reviewer is still working through
the list, and every later stage reads the status rather than the file.

Every other stage's `--force` needs no backwards edge: it recomputes the same
kind of artifact and ends in the state it was already in. Review is different
because what it holds is not derived from anything.

### Decision

`RE_ENTRY_TRANSITIONS` holds exactly one pair:

```text
REVIEWED -> READY_FOR_REVIEW
```

Written as an explicit pair rather than as a general "one step back" rule. A
general rule would also have permitted `READY_FOR_REVIEW -> ANALYZED` and
`RENDERED -> REVIEWED`, for reasons nobody decided on.

`RunStage` gains `PREVIEW` and `REVIEW`, with `FAILED_PREVIEW` and
`FAILED_REVIEW` beside them, so `READY_FOR_REVIEW` and `REVIEWED` are produced
by stages that own them instead of appearing on the success path with no stage
behind them.

`test_run_state.py` asserts the exhaustive property — that this is the only
transition anywhere in the table that moves backwards along `SUCCESS_PATH` — so
a second backwards edge added anywhere fails the suite rather than passing
quietly.

### Consequences

- A failing encoder produces `FAILED_PREVIEW`. It is no longer possible to
  record a preview failure as `FAILED_ANALYSIS`, which would have described an
  external call that never happened, or to leave a manifest asserting
  `READY_FOR_REVIEW` after previews that were not produced.
- Quitting a review, reaching end of input and pressing Ctrl+C are not failures
  and touch neither the status nor the failure record. `FAILED_REVIEW` exists
  for a technical failure of the stage, not for a person stopping.
- **A run that has been previewed can no longer be re-analysed.**
  `READY_FOR_REVIEW -> ANALYZED` is not an allowed transition, so
  `analyze --force` on such a run is refused with a state-machine error rather
  than silently stranding the previews and decisions built on the old shortlist.
  This is a real usability cost and it is deliberate for now: cascading
  invalidation is CE-052's job, and until it exists refusing is safer than
  orphaning. The workaround is a new run.
- The re-entry edge is taken only by `review --force`, and only after a warning
  that names how many decisions will be discarded.

---

## ADR-031 — Preview publication is durable, not atomic

**Status:** Accepted

### Context

Publishing a preview set replaces several files at once: one MP4 per candidate,
some of which must disappear when the shortlist changed, plus
`config.effective.json` and `index.json`. The first implementation did it in
place and in order, and a failure part-way through left the directory holding
half of one set and half of another with `index.json` describing neither — worse
than either set, because the previous previews had been reusable and were now
unverifiable.

The fix was to move the whole published set into `previews/.rollback/`, assemble
the new one, and delete the backup last, restoring it on failure. That made the
ordinary failure atomic. It also introduced a worse defect than the one it
fixed: the backup was deleted unconditionally on the way out of `generate`, so
a failure **during the restore** destroyed the only surviving copy and left
`previews/` empty. Three separate places deleted the backup and none of them
checked whether the restore had finished.

That is the honest starting point for this decision: a restore is a sequence of
renames, a rename can fail for reasons that have nothing to do with this program
— a full disk, a revoked permission, a scanner holding a handle — and no
ordering makes an operation that cannot complete complete. Atomicity is not
available. Pretending otherwise is how the second defect got written.

### Decision

**The guarantee is durability, and it is stated as such.** Three outcomes, not
two:

1. the new set is published;
2. publication failed and the previous set is restored byte for byte;
3. publication failed and the restore could not finish — in which case every
   file of the previous set is in `previews/` or in `previews/.rollback/`, the
   backup is not deleted, the error names the directory holding the data, and
   the next invocation finishes the restore.

Outcome 3 is not atomic and is not described as though it were. The directory is
temporarily incomplete; nothing is lost.

**A backup is deleted in exactly two places**, both of which are reached only
after the thing they conclude has provably finished: after publication has
placed every file, and after a restore has moved every file back. The
unconditional `rmtree` in `generate`'s `finally` is gone.

**A pre-existing backup is never overwritten or blindly removed.**
`resolve_pending_rollback` runs before anything else in `generate`, and
`_publish` refuses outright if one is still there. The single exception is a
backup directory that is empty *and* has no journal: it holds nothing
recoverable, and that is checked rather than assumed.

**The phase is journalled.** `previews/.rollback/rollback.json` records how far
publication had got, because the undo for the two phases is the opposite of the
other. In `moving_aside` the previews directory still holds part of the previous
set, so the undo moves files back and deletes nothing. In `placing` every old
file is already in the backup, so anything publishable left in the directory
belongs to the failed attempt and is deleted first. Applying the wrong one
deletes exactly the files that have no second copy — which is a defect an
earlier version of this code actually had, when it inferred the answer from the
directory contents instead. One flag suffices because the phases are disjoint by
construction: nothing is placed until everything has been moved aside.

A journal this build cannot read is a refusal, never a default. The phase
decides which files get deleted, so guessing it is worse than doing nothing.

### Considered and rejected: publishing by directory rename

Build `previews.new`, rename `previews` to `previews.old`, rename `previews.new`
to `previews`, drop `previews.old`. Two renames instead of a dozen, and the
suggestion is a good one. It was rejected on three grounds.

**Windows holds directories.** A directory rename fails while any handle is open
to the directory or to a file inside it. The entire point of this stage is that
a person opens these previews in a video player, so the swap would fail exactly
when the feature is being used. A per-file rename fails only for the file
actually held, and the restore then puts everything back.

**It is not atomic either.** Between the two renames there is no `previews`
directory at all. A crash there leaves the run without a directory
`RunWorkspace` created and that both `review` and the manifest's stage record
reference — a worse state than an incomplete one, and one no later invocation
could tell apart from a run that had never been previewed.

**Same-filesystem renames are already guaranteed.** Keeping `.staging` and
`.rollback` inside `previews/` is what makes every move a rename on one
filesystem on both platforms, which is why the restore needs no space and cannot
fail for want of any. Making them siblings of `previews/` under the run
directory would preserve that too, and buys nothing else.

### Consequences

- A failed `--force` still costs nothing in the common case: the previous
  previews are restored and still pass `verify_previews`.
- A failed restore costs an incomplete directory and a message, not data. The
  operator is told which directory holds the files, and `preview` finishes the
  job on the next run.
- `PreviewRollbackError` exists so this is distinguishable from an ordinary
  render failure. An ordinary one can just be retried; this one means files are
  sitting somewhere other than where they belong.
- The previews directory can legitimately contain a `.rollback` subdirectory
  between runs. Nothing downstream scans for extra files — `require_previews`
  reads the index and the names in it — so a pending backup does not make a
  complete set look invalid.
- `review` does not resolve a pending backup. It must not mutate the previews
  directory, so it fails verification with a message pointing at `preview`,
  which is the command that owns those files.
