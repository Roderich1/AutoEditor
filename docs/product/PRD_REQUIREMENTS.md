# Content Engine — Product Requirements Document (PRD)

## 1. Product statement

Content Engine is a personal-first system that reduces the manual work required to transform a long technical recording into several useful short-form clips.

## 2. Primary problem

After recording, creators must re-watch content, locate useful moments, define clip boundaries, cut/reframe video, create subtitles and prepare metadata. This post-production workflow consumes disproportionate time.

## 3. Primary user

Initial primary user: a technical creator documenting learning/projects in Linux, DevOps, cloud, AI, programming, networking and automation.

## 4. Primary outcome

Given a long source video, the user should receive a ranked set of strong candidate segments, rapidly approve/edit them, and obtain vertical subtitled renders with less manual work than a conventional workflow.

## 5. Non-goals for V0

V0 is not a social network scheduler, SaaS, collaborative editor, cloud transcoding farm or autonomous agent platform.

---

# 6. Functional requirements — V0

## FR-001 — Media input

The system shall accept a local source video path through CLI.

**Acceptance:** valid source is registered into a run; missing/unreadable source fails clearly.

## FR-002 — Media inspection

The system shall inspect actual media streams using ffprobe.

**Acceptance:** duration, video codec/resolution/fps, audio stream information and container data are persisted.

## FR-003 — Audio extraction

The system shall create a normalized transcription audio file.

**Acceptance:** valid source with audio produces expected WAV and the stage is logged.

## FR-004 — Timestamped transcription

The system shall transcribe speech with segment and word timestamps.

**Acceptance:** canonical transcript JSON contains absolute source timestamps and exports TXT/SRT.

## FR-005 — Transcript chunking

The system shall divide long transcripts into overlapping analysis chunks while preserving absolute timestamps.

## FR-006 — AI candidate generation

The system shall request structured candidate proposals from an AI analyzer.

**Acceptance:** output conforms to the candidate schema or fails as an analysis error; raw responses are retained when appropriate.

## FR-007 — Prompt safety

Transcript content shall be treated as untrusted data and shall not override the analyzer instructions.

## FR-008 — Candidate validation

The system shall reject invalid time ranges, out-of-source ranges and durations outside configured limits.

## FR-009 — Boundary adjustment

The system shall attempt to align AI-proposed boundaries to nearby natural speech boundaries.

## FR-010 — Deterministic candidate score

The system shall compute final candidate score in code from dimension scores using the documented weighting formula.

## FR-011 — Candidate deduplication

The system shall remove/merge equivalent overlapping candidate suggestions according to temporal overlap rules.

## FR-012 — Candidate ranking

The system shall present a configurable top-N ranked candidate list.

## FR-013 — Candidate preview

The system shall generate lightweight preview clips before final approval.

## FR-014 — Human approval

The system shall allow approve, reject and boundary-edit decisions for each candidate.

## FR-015 — Decision persistence

The system shall persist original boundaries, final boundaries, decision and optional rejection reason.

## FR-016 — Clip-local subtitles

The system shall derive approved-clip subtitle timings from source transcript timings.

## FR-017 — SRT export

The system shall generate an SRT file for each approved clip.

## FR-018 — ASS export

The system shall generate an ASS subtitle file suitable for burned styling.

## FR-019 — Vertical blur render

The system shall support a 9:16 render preserving the original frame over a blurred fill background.

## FR-020 — Vertical crop render

The system shall support a center-crop 9:16 preset.

## FR-021 — Final output

The system shall produce a clean MP4 master for each approved clip with configured audio/video parameters.

## FR-022 — Run manifest

The system shall record enough source/config/model/prompt/code metadata to identify how an output was produced.

## FR-023 — Effective configuration

The exact resolved configuration for every run shall be persisted.

## FR-024 — Structured logging

Every material pipeline stage shall produce structured run-scoped events.

## FR-025 — Idempotent stages

Completed valid stages shall not repeat unnecessarily.

## FR-026 — Force rerun

The user shall be able to force a stage rerun and invalidate dependent outputs correctly.

## FR-027 — Resume

The user shall be able to resume an interrupted run from the earliest unfinished/stale stage.

## FR-028 — Run report

The system shall produce machine-readable and human-readable reports containing timings and candidate funnel metrics.

## FR-029 — Evaluation against ground truth

The system shall support comparison between generated candidates and manually defined reference clips using temporal overlap metrics.

## FR-030 — Diagnostic command

The system shall provide a doctor command that validates the environment before expensive processing.

---

# 7. Functional requirements — V1 (planned, not V0)

## FR-V1-001 — Web upload/selection

Provide a browser UI for source video ingestion.

## FR-V1-002 — Persistent relational state

Persist videos, transcripts, analysis runs, candidates, decisions and render jobs in PostgreSQL.

## FR-V1-003 — Background jobs

Move long processing out of HTTP request lifetimes.

## FR-V1-004 — Candidate review UI

Provide preview, scores, reasoning and boundary editing in a usable web experience.

## FR-V1-005 — Render job status

Expose queued/running/success/failed state.

## FR-V1-006 — Metadata generation

Create platform-adaptable title/description/hashtag variants.

V1 explicitly does not require Kubernetes/microservices.

---

# 8. Planned distribution requirements — V2

- adapter isolation by platform;
- OAuth/permissions according to current platform requirements;
- publication status tracking;
- retries/idempotency where platform semantics permit;
- clean master and platform-specific variants;
- re-verify all current API rules at implementation time.

---

# 9. Non-functional requirements

## NFR-001 — Reproducibility

A run must identify the source/config/model/prompt/code inputs that generated it.

## NFR-002 — Determinism where possible

Scoring arithmetic, validation, deduplication, ranking rules and media execution should be deterministic for the same inputs/configuration.

## NFR-003 — Provider independence

Domain logic shall not depend directly on faster-whisper/Gemini/FFmpeg SDK-specific types beyond adapter boundaries.

## NFR-004 — Security

Secrets shall not be committed/logged. Transcript content is untrusted. LLM output cannot directly execute shell commands.

## NFR-005 — Observability

Failures shall identify stage, run and meaningful cause.

## NFR-006 — Recoverability

Runs should resume without recomputing valid earlier stages.

## NFR-007 — Testability

Normal CI must use fake AI/transcription adapters and avoid paid/network dependency.

## NFR-008 — Maintainability

Architecture changes require documented rationale rather than silent technology additions.

## NFR-009 — Performance measurement

Do not optimize before measuring. Pipeline stage durations must be observable.

## NFR-010 — Data integrity

Downstream artifacts cannot be silently reused after an upstream dependency changes.

## NFR-011 — Media safety

All subprocess calls use argument lists with controlled inputs; actual streams are probed before processing.

## NFR-012 — Usability target

The review process should be faster than manually scanning the complete source video.

---

# 10. User stories

## US-001 — Process a technical recording

As a technical creator, I want to process a long recording so that I do not have to manually search the entire video for short-form moments.

## US-002 — Understand why a clip was suggested

As a creator, I want a candidate's topic/category/scores/reason so I can judge it quickly.

## US-003 — Preserve editorial control

As a creator, I want to approve/reject/edit before final render so AI does not publish low-quality content on my behalf.

## US-004 — Correct boundaries

As a creator, I want to adjust start/end when the candidate idea is good but the cut is awkward.

## US-005 — Obtain ready-to-use vertical output

As a creator, I want a 9:16 MP4 and subtitles so I can publish without performing the entire edit manually.

## US-006 — Compare experiments

As the developer, I want model/prompt/config/run metrics so I can objectively evaluate improvements.

## US-007 — Recover after failure

As the developer, I want a run to resume without redoing completed expensive work.

---

# 11. Product success criteria

V0 should generate evidence that:

1. transcription is sufficiently accurate for technical content;
2. candidate generation finds useful segments;
3. the majority of useful candidates require limited boundary correction;
4. duplicate suggestions are manageable;
5. final renders/subtitles are reliable;
6. review + render requires much less manual time than manual discovery/editing;
7. results are reproducible enough to run prompt/model experiments.

Candidate Acceptance Rate >=60% is an aspirational early target, not a hard claim. The first experiments exist to establish the real baseline.
