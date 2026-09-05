# CONTENT ENGINE
## Master Product and Engineering Specification

**Document purpose:** complete source of truth for product intent, scope, architecture, quality criteria and evolution.

**Working name:** Content Engine  
**Current stage:** design complete / ready for V0 implementation  
**Initial mode:** personal tool, local-first technical core  
**Long-term direction:** AI Content Intelligence Platform

---

# 1. Executive summary

Content Engine is a platform for transforming long-form technical recordings into multiple high-quality short-form content pieces with substantially less manual editing work.

The creator records real learning, projects, mistakes, debugging, explanations and solutions. Content Engine then:

1. inspects the media;
2. extracts/normalizes audio;
3. transcribes speech with timestamps;
4. divides the transcript into semantic/temporal analysis chunks;
5. uses AI to propose potentially useful short segments;
6. validates, scores, deduplicates and ranks those candidates;
7. lets the creator approve, reject or edit them;
8. generates synchronized subtitles;
9. renders vertical video deterministically with FFmpeg;
10. later generates platform-specific metadata, publishes, collects analytics and learns from actual performance.

The project does **not** begin as a fully autonomous social-media platform. It begins by proving one difficult thing well:

> Given a long technical video, can the system find and produce several segments the creator genuinely wants to publish while meaningfully reducing manual work?

The main risk is therefore **candidate quality**. UI polish, cloud infrastructure and social integrations are secondary until that risk is validated.

---

# 2. Origin and motivation

The project comes from a real personal workflow: publicly document the progression from software/full-stack development into Linux, Ubuntu Server, DevOps, cloud, AI, networking and automation while building practical projects.

Traditional content production introduces an expensive second job after recording:

```text
record
  ↓
watch everything again
  ↓
find interesting moments
  ↓
cut clips
  ↓
reframe for vertical
  ↓
create subtitles
  ↓
correct subtitles
  ↓
write titles/descriptions/hashtags
  ↓
export
  ↓
upload to each network
```

A one-hour technical recording can create several additional hours of post-production. Many steps are mechanical and can be assisted by deterministic automation or AI.

Content Engine aims to compress the distance between:

```text
"I finished recording"
```

and:

```text
"I have several clips worth publishing"
```

---

# 3. Product vision

The eventual ecosystem is:

```text
LEARN
  ↓
BUILD / DEBUG / EXPLAIN
  ↓
RECORD
  ↓
CONTENT ENGINE
  ↓
SHORT-FORM CONTENT
  ↓
PUBLISH
  ↓
AUDIENCE + PERFORMANCE DATA
  ↓
LEARN WHAT WORKS
  ↓
BETTER FUTURE RECOMMENDATIONS
```

The product should create a positive loop where the development of Content Engine itself can become content:

```text
learn AI/DevOps
→ improve Content Engine
→ record the improvement
→ Content Engine processes the recording
→ publish the result
→ collect feedback/data
→ improve the system
```

This makes the project simultaneously:

- a useful personal tool;
- a strong portfolio project;
- a vehicle for technical learning;
- a content production system;
- a possible future SaaS if validated.

---

# 4. Target audience

## 4.1 Initial user

The initial user is the creator/developer himself. This is deliberate: building for one real user produces faster feedback than prematurely generalizing for unknown customers.

## 4.2 Initial content domain

The system is optimized first for educational/technical creator content such as:

- Linux;
- Ubuntu Server;
- DevOps;
- Docker;
- Kubernetes as a learning topic (not as V0 infrastructure);
- cloud computing;
- networking;
- AI;
- backend development;
- full-stack development;
- automation;
- real debugging and troubleshooting;
- project demonstrations.

## 4.3 Future users

Only after personal validation should the system consider:

- other technical creators;
- educators;
- consultants;
- developer advocates;
- teams producing long educational recordings.

---

# 5. Product philosophy

The content philosophy is:

> Document real progression, real projects, real mistakes, real debugging, real learning and real solutions.

The system should help surface authentic moments rather than fabricate artificial “viral” content.

Preferred clip patterns include:

- problem → solution;
- error → learning;
- explanation;
- quick tutorial;
- discovery;
- strong but honest opinion;
- concrete result;
- before/after;
- practical tip;
- demonstration;
- short technical story.

Avoid language such as “the AI detects viral moments”. Virality cannot be guaranteed. Preferred language:

- “segments with high engagement potential”;
- “strong short-form candidates”;
- “high-value candidate segments”.

---

# 6. Engineering algorithm adapted to the project

The project follows this sequence:

## 6.1 Question every requirement

For every feature ask:

- What real user problem does this solve?
- Is it needed to validate the core hypothesis?
- Who owns this requirement?
- What evidence says it must exist now?

## 6.2 Delete aggressively

Remove features that do not help prove the core value.

Items intentionally deleted from V0:

- frontend;
- database;
- Redis/queue infrastructure;
- authentication;
- multi-user support;
- social publishing;
- n8n;
- cloud infrastructure;
- microservices;
- Kubernetes;
- AI thumbnail generation;
- sophisticated analytics;
- autonomous agents.

## 6.3 Simplify what remains

Use a local CLI, filesystem experiment runs, ports/adapters and deterministic media processing.

## 6.4 Build and measure

Measure candidate quality, time, errors and acceptance before optimization.

## 6.5 Accelerate bottlenecks

Only after measurement optimize transcription, model calls, rendering, GPU use, cache or parallelism.

## 6.6 Automate validated workflows

Do not automate publishing before clip quality and review behavior are known.

## 6.7 Scale only when required

Kubernetes or distributed infrastructure is a response to measured scale, not a badge of professionalism.

---

# 7. Core product hypothesis

The core hypothesis is:

> Transcription + semantic analysis + structured candidate selection + deterministic validation/ranking + human review + FFmpeg rendering can reduce the manual effort required to turn a long technical recording into several usable short-form clips.

This hypothesis should be tested before product infrastructure is expanded.

---

# 8. North Star and supporting metrics

## 8.1 Time-to-Ready-Content

Time from the beginning of processing to a set of approved rendered clips ready for publication.

## 8.2 Manual Editing Time Saved

```text
manual_editing_time_saved =
manual_baseline_minutes - actual_manual_minutes_with_content_engine
```

## 8.3 Candidate Acceptance Rate (CAR)

```text
CAR = approved_candidates / presented_candidates
```

This is the principal V0 AI/product quality signal.

## 8.4 Candidate Edit Rate

Percentage of candidates whose idea is useful but start/end boundaries require manual modification.

## 8.5 Rejection reasons

Track reasons such as:

- poor context;
- weak hook;
- not useful;
- bad boundary;
- duplicate;
- too long/short;
- transcript error;
- other.

## 8.6 Render success rate

Valid approved clips that render successfully.

## 8.7 Cost metrics

Later/when external AI is used:

- AI cost per source video;
- AI cost per accepted clip;
- tokens per analysis;
- model latency.

---

# 9. Product evolution

## V0 — Technical Core

A local CLI validates the pipeline.

## V1 — Product MVP

Adds API, UI, persistent DB and asynchronous processing.

## V1.x — Quality upgrades

Metadata, subtitles, smarter reframing, brand system.

## V2 — Distribution

Publishing adapters per platform.

## V3 — Analytics

Normalize platform performance data.

## V3.5/V4 — Content Intelligence

Relate clip features to performance and improve recommendations.

## V5 — SaaS if validated

Multi-user/tenant/billing only after strong evidence.

## V6 — Scale if required

Distributed/GPU/cloud orchestration only when justified.

---

# 10. MVP functional flow

The first product version after V0 should preserve this logical flow:

```text
video
 ↓
transcription
 ↓
semantic analysis
 ↓
candidate detection
 ↓
validation
 ↓
score
 ↓
deduplication
 ↓
ranking
 ↓
human review
 ↓
render
 ↓
subtitles
 ↓
metadata
 ↓
export
```

Social distribution is a later adapter layer.

---

# 11. Human-in-the-Loop model

Human control is an intentional product feature, not a temporary defect.

Initial policy:

```text
AI proposes
   ↓
human approves / rejects / edits
   ↓
deterministic renderer executes
```

Reasons:

1. Candidate quality is uncertain during early experiments.
2. Human review protects brand quality.
3. Decisions become labeled evaluation data.
4. The system can separately learn whether it finds the right idea and whether it chooses the right boundaries.

Potential future automation must be earned with data.

---

# 12. Candidate Intelligence Engine

The Candidate Intelligence Engine is the intellectual center of the product.

Its purpose is not merely to summarize a transcript. It must identify time-bounded, self-contained segments that can function as useful short-form content.

## 12.1 Candidate quality dimensions

### Hook

Does the beginning generate curiosity/attention without dishonest clickbait?

### Value

Does the segment teach, solve, demonstrate or communicate something useful?

### Context Independence

Can a viewer understand it without having watched the full recording?

### Clarity

Is the explanation understandable and coherent?

### Engagement Potential

Is there natural tension, curiosity, relevance or relatability that could keep attention?

### Relevance

Does it align with the creator's technical content strategy?

## 12.2 Deterministic total score

The LLM may rate dimensions, but Python computes the total:

```text
Score =
0.25 * Hook
+ 0.20 * Value
+ 0.20 * Context Independence
+ 0.15 * Clarity
+ 0.10 * Engagement Potential
+ 0.10 * Relevance
```

All dimensions use 0–100.

## 12.3 Initial constraints

Defaults, configurable:

- minimum duration: 20 s;
- maximum duration: 90 s;
- minimum score: 65;
- target candidates: 10;
- maximum candidates: 15;
- deduplication IoU: 0.60;
- boundary snap window: ~±2.5 s.

## 12.4 Candidate schema concept

```json
{
  "start": 754.2,
  "end": 802.8,
  "category": "problem_solution",
  "topic": "Docker networking",
  "hook": "Este error de Docker me tomó una hora resolverlo",
  "summary": "Explica un problema de red y su solución.",
  "reason": "Problema real seguido por una solución concreta.",
  "scores": {
    "hook": 92,
    "value": 88,
    "context": 96,
    "clarity": 93,
    "engagement": 84,
    "relevance": 90
  },
  "warnings": []
}
```

The final score is added by deterministic code.

---

# 13. Transcript and semantic analysis

## 13.1 Transcription source of truth

The canonical transcript is structured JSON containing timestamped segments and words.

Text/SRT are exports; JSON remains authoritative.

## 13.2 No semantic rewriting during normalization

Initial normalization may:

- trim spaces;
- collapse duplicate whitespace;
- remove empty segments;
- normalize line endings.

It should not silently rewrite what was said.

## 13.3 Chunking

Long transcripts should be processed in overlapping windows. Baseline:

```text
window: 360 seconds
overlap: 30 seconds
```

This reduces context size while protecting ideas that cross boundaries.

## 13.4 Deduplication

Overlap introduces duplicate recommendations. Temporal Intersection over Union (IoU) removes high-overlap duplicates.

## 13.5 Boundary snapping

AI timestamps are adjusted to nearby natural word/segment boundaries. This separates semantic discovery from exact editorial timing.

---

# 14. Media processing strategy

FFmpeg/ffprobe are the deterministic media layer.

## 14.1 ffprobe

Used for:

- stream detection;
- duration;
- resolution;
- codecs;
- frame rate;
- audio presence;
- container metadata.

Do not trust file extensions.

## 14.2 Audio normalization for transcription

Baseline extraction produces mono PCM WAV at 16 kHz for a consistent transcription input.

## 14.3 Final format

Initial clean master:

- MP4;
- H.264;
- AAC;
- 1080 × 1920;
- 9:16.

## 14.4 Render presets

### vertical_blur

Source content scaled to fit while a blurred version fills the vertical canvas. Preferred for screen-heavy technical recordings because it preserves more code/terminal/interface information.

### vertical_crop

Center crop for cases where the subject is naturally centered.

Smart reframing is deferred until there is evidence it is needed.

---

# 15. Subtitle strategy

Generate:

- SRT for portable/editable subtitles;
- ASS for stylized rendering;
- optionally burned subtitles in the final render.

Clip-local timestamps are derived from source timestamps:

```text
clip_word_time = source_word_time - approved_clip_start
```

Baseline visual rules:

- maximum two lines;
- readable grouping rather than one word per screen;
- line breaks by punctuation/pauses where possible;
- approximately 6–8 visible words as a starting rule;
- safe placement for vertical video.

Advanced kinetic subtitles are later work.

---

# 16. Branding strategy

Do not permanently burn a universal brand treatment into every master.

Prefer:

```text
clean master
   ↓
optional platform/brand variant
```

Reasons:

- social platform policies and conventions differ;
- a clean master is reusable;
- branding can evolve without re-extracting the clip;
- platform adapters can enforce current requirements.

---

# 17. Metadata strategy

Metadata comes after candidate/render quality.

Future outputs per platform can include:

- title;
- description/caption;
- hashtags;
- summary;
- alternative hooks.

Do not use one universal description for every platform. Introduce platform-specific adapters.

---

# 18. Publishing architecture (future)

Publishing must be isolated from the core:

```text
Publisher
├── YouTubePublisher
├── TikTokPublisher
├── InstagramPublisher
└── FacebookPublisher
```

Conceptual interface:

```text
publish(content, metadata)
get_status(external_id)
delete_or_unpublish(external_id) when supported
```

At implementation time, re-check current API scopes, audits, limits, upload formats and branding/content requirements. Do not rely on old assumptions.

---

# 19. n8n role (future)

n8n is not the Content Engine core.

Appropriate future responsibilities:

- notifications;
- integration glue;
- schedule-based workflows;
- post-publication tasks;
- reports;
- webhooks;
- external system coordination.

Inappropriate responsibilities:

- core transcription;
- core candidate logic;
- FFmpeg render engine;
- primary domain state.

---

# 20. V0 architecture

V0 uses a simplified hexagonal/modular structure:

```text
CLI
 │
 ▼
Application Services
 │
 ▼
Domain
 │
 ▼
Ports
 │
 ▼
Adapters
```

Key ports:

- `TranscriberPort`;
- `ContentAnalyzerPort`;
- `RendererPort`.

Key adapters:

- ffprobe;
- FFmpeg;
- faster-whisper;
- OpenAI initial analyzer;
- filesystem persistence.

The domain must not import provider-specific SDKs.

---

# 21. V1 architecture

If V0 succeeds, V1 becomes a **modular monolith + background workers**.

Recommended initial stack:

- FastAPI backend;
- Next.js/React + TypeScript frontend;
- PostgreSQL;
- Redis + worker abstraction;
- local storage first, S3-compatible abstraction later;
- Docker Compose.

Do not introduce Kafka, service mesh or Kubernetes without concrete requirements.

Conceptual V1:

```text
Browser
  ↓
Next.js
  ↓
FastAPI
  ├── PostgreSQL
  ├── Redis/jobs
  └── Storage
        ↑
      Worker
      ├── Transcription
      ├── AI analysis
      └── FFmpeg
```

---

# 22. Future data model

Likely V1 entities:

- User;
- Project;
- Video;
- Transcript;
- TranscriptSegment;
- AnalysisRun;
- ClipCandidate;
- ReviewDecision;
- Clip;
- RenderJob;
- Subtitle;
- MetadataVariant;
- PlatformAccount;
- Publication;
- AnalyticsSnapshot.

## 22.1 AnalysisRun importance

Every AI run should record:

- provider;
- model;
- prompt version/hash;
- input hash;
- configuration;
- timing;
- token/cost information where available;
- status.

This supports real evaluation rather than anecdotal changes.

---

# 23. V0 filesystem data model

Each run is an experiment:

```text
workspace/runs/<RUN_ID>/
├── manifest.json
├── config.effective.json
├── media/probe.json
├── audio/source.wav
├── transcript/transcript.json
├── transcript/transcript.txt
├── transcript/transcript.srt
├── analysis/chunks.json
├── analysis/candidates.raw.json
├── analysis/candidates.json
├── analysis/raw/...
├── review/decisions.json
├── previews/...
├── clips/...
├── logs/run.jsonl
├── report.json
└── report.md
```

A run manifest should record source/config/model/prompt/code versions and hashes.

---

# 24. Run states

Baseline:

```text
CREATED
→ INSPECTED
→ AUDIO_READY
→ TRANSCRIBED
→ ANALYZED
→ READY_FOR_REVIEW
→ REVIEWED
→ RENDERED
→ COMPLETED
```

Failure states should identify the failed stage. Resume should continue from the latest valid stage.

---

# 25. Idempotency and staleness

Completed stages should not rerun unless needed.

Example:

```text
transcription already valid
→ skip
```

`--force` may regenerate it.

If transcription changes, downstream analysis/review/render artifacts become stale. The system must not silently combine incompatible artifacts.

Fingerprints should include relevant source/config/model/code inputs.

---

# 26. Error handling

Domain exception hierarchy should distinguish:

- invalid configuration;
- invalid media;
- missing audio;
- transcription failure;
- AI provider failure;
- invalid candidate output;
- render failure.

Transient external errors may retry with exponential backoff + jitter. Permanent errors should fail fast.

---

# 27. Security

## 27.1 Transcript is untrusted input

Spoken content can contain text that resembles instructions. System prompts must state that transcript content is data and cannot alter agent rules.

## 27.2 LLM cannot execute commands

Flow:

```text
LLM structured output
→ Pydantic validation
→ domain rules
→ deterministic request
→ FFmpeg
```

Never:

```text
LLM writes arbitrary shell command
→ execute it
```

## 27.3 FFmpeg subprocess safety

Use argument arrays and avoid shell concatenation of user input.

## 27.4 Secrets

API keys/tokens never enter the repository or logs.

## 27.5 Media validation

Inspect actual streams with ffprobe; do not trust names/extensions.

---

# 28. Logging and observability

Use structured logs in V0. Example event classes:

- `run.created`;
- `media.inspected`;
- `audio.extracted`;
- `transcription.started/completed`;
- `analysis.chunk.started/completed`;
- `candidate.rejected_validation`;
- `candidate.deduplicated`;
- `review.decision`;
- `render.started/completed`;
- `stage.failed`;
- `run.completed`.

Each event should include `run_id` and relevant identifiers/timing.

---

# 29. Testing strategy

## 29.1 Unit tests

Required around deterministic domain behavior:

- score formula;
- candidate duration/timestamp validation;
- IoU;
- deduplication;
- ranking;
- boundary snapping;
- SRT/ASS time conversion;
- run state transitions;
- hashes/fingerprints.

## 29.2 Integration tests

Exercise real ffprobe/FFmpeg for:

- media inspection;
- audio extraction;
- clip cutting;
- vertical composition;
- subtitle burn-in.

## 29.3 AI/transcription tests

Normal CI must not depend on paid/network model calls. Use fake adapters.

Optional slow/AI tests can be explicitly enabled.

## 29.4 End-to-end deterministic fixture

Small real video fixture + fake transcript/analyzer + real FFmpeg should prove architecture end to end.

---

# 30. Evaluation strategy

Create a small golden dataset using real videos.

For each video, manually define good clip intervals. Compare AI candidates using temporal IoU and human acceptance.

Metrics:

- precision-like candidate usefulness;
- recall against human-marked segments;
- average temporal IoU;
- start boundary error;
- end boundary error;
- CAR;
- edit rate;
- rejection reasons;
- processing time;
- cost.

The golden dataset should grow over time.

---

# 31. Quality gates

Do not consider an implementation complete if tests/lint/type checks fail.

Suggested baseline:

- Ruff lint/format;
- mypy;
- pytest;
- >80% coverage target specifically around domain/services, without chasing artificial 100%;
- CI with no paid AI dependency;
- real smoke tests for media operations.

---

# 32. V0 Definition of Done

V0 is complete when a command-line demonstration can reliably perform:

```text
source video
→ inspection
→ normalized audio
→ timestamped transcript
→ candidate generation
→ structured validation
→ deterministic scoring
→ boundary snapping
→ deduplication
→ ranking
→ previews
→ human review
→ SRT/ASS
→ vertical renders
→ report
```

And when:

- it works on several real technical videos;
- no invalid timestamps reach render;
- duplicate overlap is controlled;
- subtitles are acceptably synchronized;
- runs can resume;
- stages are idempotent;
- candidate quality is useful enough to justify a UI;
- manual effort is meaningfully reduced.

---

# 33. V1 Definition of Success

V1 should not be judged by number of features. It succeeds if the creator can upload/process a video, rapidly review good candidates, render selected clips, and spend much less manual time than the traditional workflow.

---

# 34. Future analytics / Content Intelligence

Once publishing and analytics exist, derive a `ContentFingerprint` per clip. Example features:

```json
{
  "topic": "Docker",
  "category": "problem_solution",
  "duration": 37,
  "hook_strength": 93,
  "technical_depth": 74,
  "narrative_pattern": "frustration_to_solution"
}
```

Relate those features to normalized platform performance.

Possible future recommendations:

- “Problem → solution clips about Docker retain viewers better than general explanations.”
- “Your strongest completion range is 25–40 seconds.”
- “This new source video contains three segments similar to your historically strongest pattern.”

This creator-specific feedback loop can become a real differentiator.

---

# 35. Portfolio value

Even before SaaS, Content Engine can demonstrate:

- Python/backend design;
- REST/API design in V1;
- AI integration;
- prompt engineering;
- structured outputs;
- multimedia/FFmpeg;
- asynchronous job processing;
- PostgreSQL/Redis in V1;
- React/Next.js in V1;
- Docker/Linux;
- testing;
- observability;
- CI/CD;
- security;
- OAuth in V2;
- architecture and ADR discipline;
- DevOps evolution without unnecessary complexity.

The project is more valuable as a portfolio piece if it shows measured engineering decisions rather than a large pile of technologies.

---

# 36. Non-goals and forbidden shortcuts

Until evidence changes them:

- no microservices for prestige;
- no Kubernetes for prestige;
- no “AI decides and publishes everything” in early versions;
- no database in V0;
- no platform integration before the candidate/render core works;
- no LLM-controlled shell execution;
- no unversioned prompts;
- no hiding weak candidate quality behind a polished frontend;
- no claiming virality prediction;
- no silent architectural changes by future agents.

---

# 37. Change-control rule for future AIs/developers

A major design decision may change only when:

1. a concrete current problem is demonstrated;
2. alternatives are compared;
3. the reason is documented;
4. consequences are documented;
5. an ADR is added/updated;
6. the change does not undermine reproducibility/evaluation without a replacement strategy.

---

# 38. Immediate next step

Do not continue brainstorming broad future features. Start V0 Milestone V0.1:

```text
CE-001 → CE-010
```

The first public/working slice should eventually support:

```text
content-engine doctor
content-engine inspect sample.mp4
content-engine run sample.mp4
```

Then move through the critical path in `ROADMAP_BACKLOG.md` and the detailed implementation contracts in `V0_IMPLEMENTATION_SPEC.md`.
