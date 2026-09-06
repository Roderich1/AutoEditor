# Content Engine — Roadmap and Ordered Backlog

## Roadmap overview

### V0 — Technical Core

Goal: prove long video → candidate clips → human review → rendered shorts works reliably enough to justify a product UI.

### V1 — Product MVP

Add FastAPI, PostgreSQL, Redis/background workers, Next.js/React UI, upload, dashboard, candidate review UI, render job tracking, local storage abstraction.

### V1.1 — Metadata

Add title/description/hashtag variants and structured metadata generation.

### V1.2 — Better Subtitles

Add advanced line breaking, styling presets, word highlighting and better subtitle-safe areas.

### V1.3 — Smart Crop

Add face/speaker/screen-aware reframing only after baseline rendering has been validated.

### V1.4 — Brand System

Add templates, fonts, clean master + optional branded variants, platform-aware safe zones.

### V2 — Distribution

Add publishing adapters one platform at a time, likely YouTube first. Re-verify each platform's current API requirements at implementation time.

### V2.5 — n8n integration

Use n8n for external workflows, notifications, scheduling and integration glue — not for core video processing.

### V3 — Analytics

Collect platform metrics where APIs permit and normalize them into Content Engine.

### V3.5 — Content Intelligence

Relate clip fingerprints/features to real performance.

### V4 — Recommendation Engine

Use historical creator-specific performance to improve candidate recommendations and content guidance.

### V5 — SaaS, only if justified

Authentication, organizations, tenant isolation, quotas, billing, object storage, distributed workers.

### V6 — Scale, only if justified

Autoscaling/GPU pools/distributed queue/cloud orchestration/Kubernetes only when measured usage demands it.

---

# V0 milestones

## V0.1 — Foundation

- **CE-001** Bootstrap Python package.
- **CE-002** Configure `uv` and Python 3.12 pin.
- **CE-003** Configure Ruff.
- **CE-004** Configure mypy.
- **CE-005** Configure pytest/coverage.
- **CE-006** Create Typer CLI skeleton.
- **CE-007** Implement configuration loader and effective config model.
- **CE-008** Implement `content-engine doctor`.
- **CE-009** Implement run workspace creation and `run_id` strategy.
- **CE-010** Implement `manifest.json` with version/hash metadata.

**Exit:** `content-engine doctor` works and a reproducible run folder can be created.

## V0.2 — Media

- **CE-011** ffprobe adapter.
- **CE-012** `MediaInfo` domain model.
- **CE-013** input validation.
- **CE-014** normalized audio extraction.
- **CE-015** media integration tests.

**Exit:** valid video can produce `probe.json` and normalized WAV.

## V0.3 — Transcription

- **CE-016** `TranscriberPort`.
- **CE-017** `FasterWhisperTranscriber`.
- **CE-018** transcript domain models.
- **CE-019** JSON transcript exporter.
- **CE-020** plain text exporter.
- **CE-021** SRT exporter.
- **CE-022** transcription timing/metrics.

**Exit:** real video produces timestamped transcript JSON/TXT/SRT.

## V0.4 — Candidate Intelligence Engine

- **CE-023** transcript chunker with overlap.
- **CE-024** candidate Pydantic/domain schemas.
- **CE-025** deterministic score engine.
- **CE-026** prompt `clip_candidates/v1`.
- **CE-027** `ContentAnalyzerPort`.
- **CE-028** initial Gemini adapter (ADR-019).
- **CE-029** structured-output parsing/validation.
- **CE-030** timestamp/duration validator.
- **CE-031** boundary snapper.
- **CE-032** temporal IoU + deduplication.
- **CE-033** global ranking/top-N selection.

**Exit:** transcript produces explainable `candidates.raw.json` and final `candidates.json`.

## V0.5 — Human Evaluation

- **CE-034** low-cost preview renderer.
- **CE-035** interactive `review` command.
- **CE-036** approve decision.
- **CE-037** reject decision + optional reason.
- **CE-038** edit candidate boundaries.
- **CE-039** persist `decisions.json`.

**Exit:** creator can review all candidates and produce persisted decisions.

## V0.6 — Final Render

- **CE-040** subtitle builder from source word timestamps.
- **CE-041** clip-local SRT generation.
- **CE-042** ASS generation.
- **CE-043** `vertical_blur` preset.
- **CE-044** `vertical_crop` preset.
- **CE-045** final FFmpeg renderer.
- **CE-046** output verification.

**Exit:** approved candidate becomes synchronized 1080x1920 MP4 + SRT + ASS.

## V0.7 — Pipeline Reliability

- **CE-047** `PipelineService`.
- **CE-048** `process` command.
- **CE-049** `resume` command.
- **CE-050** stage idempotency.
- **CE-051** fingerprints/cache validation.
- **CE-052** downstream stale dependency handling.

**Exit:** interrupted runs can resume and completed stages are not repeated unnecessarily.

## V0.8 — Evaluation

- **CE-053** ground-truth schema.
- **CE-054** temporal IoU matching.
- **CE-055** candidate precision metric.
- **CE-056** candidate recall metric.
- **CE-057** CAR/edit/rejection metrics.
- **CE-058** `report.json`.
- **CE-059** `report.md`.

**Exit:** multiple experiments can be compared quantitatively.

## V0.9 — Engineering Quality

- **CE-060** structured logging.
- **CE-061** exception hierarchy and CLI exit codes.
- **CE-062** retry policy for transient external failures.
- **CE-063** integration/e2e tests with fake AI/transcription.
- **CE-064** CI pipeline.
- **CE-065** CPU Docker image after bare-metal success.
- **CE-066** README/usage documentation.
- **CE-067** architecture documentation refresh.

**Exit:** V0 is repeatable, testable and handoff-ready.

---

# Critical path

Build in this order unless a concrete blocker forces a change:

```text
project foundation
→ run workspace
→ ffprobe
→ audio extraction
→ transcription
→ transcript JSON
→ chunking
→ candidate schema
→ AI adapter
→ structured candidate output
→ deterministic score
→ boundary snapping
→ deduplication
→ ranking
→ preview
→ human review
→ subtitle generation
→ final render
→ pipeline resume/idempotency
→ evaluation
→ CI
→ Docker
```

Do not build the frontend before the Candidate Intelligence Engine is empirically useful.

---

# V0 experiment plan

Use at least five real videos representing different technical content styles:

1. Linux/Ubuntu Server task.
2. Docker/devops troubleshooting.
3. Programming/backend explanation.
4. Real debugging/problem-solving session.
5. Conceptual explanation/tutorial.

For each video, manually mark a small ground-truth set of good clips before inspecting AI results whenever possible. This reduces confirmation bias.

Compare experiments across:

- transcription model/profile;
- prompt version;
- analysis model;
- chunk size/overlap;
- score threshold;
- boundary snapping behavior.

Track:

- candidate acceptance rate;
- candidate edit rate;
- rejection reasons;
- temporal overlap with human ground truth;
- pipeline time;
- AI calls/tokens/cost;
- render success;
- manual minutes required.

---

# V0 go/no-go criteria

## GO to V1 when

- multiple real videos process end to end;
- transcript timestamps are usable;
- the candidate list contains enough genuinely useful segments;
- duplicate candidates are controlled;
- subtitle timing is acceptable;
- final vertical renders are reliable;
- human review is faster than manually searching the full video;
- metrics show meaningful manual time reduction;
- V1 UI would improve usability rather than hide a weak core.

## REWORK V0 when

- candidate acceptance is consistently poor;
- most candidates need large boundary edits;
- technical transcription errors destroy meaning;
- manual review is still almost as expensive as manual editing;
- render failures are frequent;
- configuration/model changes cannot be reproduced.
