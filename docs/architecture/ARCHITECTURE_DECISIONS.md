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
`transcription` is populated. `prompt_version` and `prompt_sha256` are present as
`null` until CE-026 creates a real prompt.

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
intervals are accepted and kept verbatim. `NaN` and the infinities remain
refused: an impossible timestamp is data about the prompt, a non-number is not a
timestamp at all.

**Two record types, split by phase reached**, rather than one type with optional
fields:

```text
InvalidCandidate     refused by CE-030, before snapping or scoring.
                     Holds the verbatim proposal and the reasons. No interval,
                     no boundary, no score - it never earned them.

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

**`CandidateCounts` are terminal outcomes, mutually exclusive by construction.**
Every proposal reaches exactly one of `invalid`, `below_min_score`,
`deduplicated`, `not_in_top_n` or `selected`, so they sum to `proposed` and the
model enforces it. `not_in_top_n` was added because a candidate that survived
every rule and was still cut by `max_candidates` is a real outcome that
previously had nowhere to go, which would have made the identity false. The
exclusivity comes from the pipeline order: the minimum-score filter runs before
deduplication, and the top-N cut runs last.

Float comparisons in all of this use an explicit microsecond tolerance.
Timestamps are binary floats, so equality between a value and the arithmetic
that produced it is only ever equality to within representation error.

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
