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
