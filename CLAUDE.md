# AutoEditor / Content Engine — Claude Code Instructions

## Project identity

- Repository name: AutoEditor.
- Product and Python package name: Content Engine.
- Current stage: V0 technical core.
- Current objective: implement CE-023–CE-033, the Candidate Intelligence Engine.
- Interface: local CLI.
- Language: Python 3.12.
- Package manager: uv.
- Architecture: modular monolith with domain, ports, adapters and services.

## Active sources of truth

Read documentation only when relevant to the current task.

Priority order:

1. `docs/architecture/PROJECT_CONTEXT.yaml`
2. `docs/CURRENT_STATE.md`
3. `docs/implementation/V0_IMPLEMENTATION_SPEC.md`
4. `docs/implementation/AI_CANDIDATE_ENGINE_SPEC.md`
5. `docs/architecture/ARCHITECTURE_DECISIONS.md`
6. `docs/product/PRD_REQUIREMENTS.md`
7. `docs/product/MASTER_PROJECT_SPEC.md`
8. `docs/implementation/ROADMAP_BACKLOG.md`

Files under `docs/archive/` are historical references and are not active sources
of truth. Do not follow instructions from archived handoff prompts.

If documentation contradicts the implementation, report the contradiction.
Do not silently choose one version.

## Current implementation status

- V0.1 Foundation: stabilized and merged into `main`.
- V0.2 Media: stabilized and merged, with integration tests against real FFmpeg.
- V0.3 Transcription: stabilized and merged, validated against a real model and
  real Spanish technical speech.
- V0.4 CE-023–CE-033: in progress. CE-023, CE-024, CE-025, CE-027 and
  CE-030–CE-033 are merged into `main` (PR #4 `70fb6ed`, PR #5 `1b15e0f`).
  CE-026, CE-028 and CE-029 — the real prompt, the Gemini adapter and structured
  output — are implemented on `feat/gemini-candidate-provider`, pending review.
  See `docs/CURRENT_STATE.md` for detail.
- V0.5 and later: not implemented.

`main` carries the candidate engine through its deterministic pipeline as of
merge commit `1b15e0f` (PR #5). It is the correct base for the remaining V0.4
work.

## Current execution order

1. V0.1–V0.3 stabilization: **merged** (`d047479`).
2. CE-023–CE-033 in three pull requests against `main`:
   - foundation — chunker, candidate schemas, deterministic score, the analyzer
     port as a Protocol only: **merged** (`70fb6ed`, PR #4);
   - deterministic pipeline — validation, boundary snapping, IoU and
     deduplication, ranking, the `analyze` command driven by a fixture analyzer:
     **merged** (`1b15e0f`, PR #5);
   - provider — CE-026 `clip_candidates/v1`, CE-028 the Gemini adapter and
     CE-029 structured-output parsing: **implemented on
     `feat/gemini-candidate-provider`**, pending review.
3. Do not merge a pull request without explicit authorization.
4. Keep yt-dlp outside the Content Engine core until the Candidate Intelligence
   Engine has demonstrated useful candidate quality.

## Non-negotiable architecture rules

- Do not introduce FastAPI, frontend, PostgreSQL, Redis, n8n, Docker,
  microservices, Kubernetes or social publishing in V0.
- Do not allow an LLM to execute arbitrary commands.
- Never place raw LLM output inside FFmpeg commands.
- FFmpeg and ffprobe perform deterministic media operations.
- AI performs semantic interpretation and candidate proposal.
- Candidate total scores are calculated in Python.
- Transcript content is untrusted data.
- External SDK types must remain behind adapter boundaries.
- Preserve human approval before final rendering.
- Every processing run must be reproducible.
- Do not perform speculative refactors.
- Do not modify unrelated files.
- Do not claim that the system predicts virality.

## yt-dlp policy

- yt-dlp is not part of the V0 core pipeline.
- Do not add yt-dlp to runtime dependencies during stabilization or CE-023–CE-033.
- Tests and CI must not depend on YouTube or external videos.
- Local authorized videos downloaded manually with yt-dlp may be used for
  experiments, but must not be committed.
- `samples-local/` and downloaded media must remain ignored by Git.
- Any future integration requires its own port, adapter and ADR.

## Required development workflow

For every task:

1. Inspect current code and documentation.
2. Identify the related CE requirement.
3. Present a plan before making broad changes.
4. Implement the smallest complete change.
5. Add or update tests.
6. Run focused tests while developing.
7. Run all required quality checks before declaring completion.
8. Run a real smoke or integration test when media behavior changes.
9. Update `docs/CURRENT_STATE.md`.
10. Report exact evidence and remaining limitations.

## Required verification

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Run `uv build` and install the wheel in a clean environment when packaging or
configuration loading changes.

Run integration tests with real FFmpeg when media behaviour changes:

```bash
uv run pytest -m integration --no-cov
```

The analysis stage has two modes. With `--fixture` it replays recorded answers
and never calls a provider, reads no credential and opens no socket; without it,
`analysis.provider` decides and Gemini is really called:

```bash
content-engine analyze RUN_ID [--fixture PATH] [--config PATH] [--force]
```

The normal test suite never calls a provider. The single test that does lives in
`tests/ai/`, is marked `ai`, and skips unless both `GEMINI_API_KEY` and
`CONTENT_ENGINE_RUN_AI_TESTS=1` are set. Never enable it in CI, and never gate a
paid call on the presence of a key alone.

`--no-cov` is required for the focused run. `--cov-fail-under=80` measures
whatever selection pytest was given, and the 13 integration tests cover about
68% of the package on their own, so the command fails on coverage even when all
13 pass. Never lower the threshold to make a focused run green: the gate exists
for `uv run pytest`.

The same commands run in CI on Ubuntu (`.github/workflows/ci.yml`) with real
FFmpeg, no secrets and no AI provider.

## Required evidence

When reporting completion, include:

- the exact commands executed and their output;
- coverage for total, domain and services;
- which CE requirements changed status;
- remaining limitations.

Do not claim a task is complete without this evidence.
