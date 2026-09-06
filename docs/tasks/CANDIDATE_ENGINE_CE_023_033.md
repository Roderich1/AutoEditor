# Candidate Engine CE-023 to CE-033

Traceability for the Candidate Intelligence Engine, split across three pull
requests against `main`. Each row names where the requirement lives and what
proves it.

## PR A — foundation (merged, `70fb6ed`, PR #4)

| CE | What landed | Where | Verified by |
|---|---|---|---|
| CE-023 | Transcript chunker: 360 s window, 30 s overlap, 330 s stride, segments atomic, timestamps absolute | `services/chunking_service.py` | `tests/unit/test_chunking.py` |
| CE-024 | Candidate domain schemas: six ratings and no total, untrusted raw output preserved, records split by the phase they reached | `domain/candidates.py`, `domain/enums.py` | `test_candidate_models.py`, `test_candidate_contracts.py` |
| CE-025 | Deterministic score: ADR-008 weights in `Decimal`, half up, versioned | `domain/scoring.py` | `tests/unit/test_scoring.py` |
| CE-027 | `ContentAnalyzerPort` as a Protocol only, over domain types | `ports/analyzer.py` | `tests/unit/test_analyzer_port.py` |
| — | One canonical serialization behind every digest | `utils/canonical.py` | `tests/unit/test_canonical.py`, digests pinned to `main@d047479` |

Decisions recorded: ADR-019 (Gemini as the initial provider), ADR-020 (candidate
records describe the phase they reached), ADR-021 (`target_candidates` is a run
objective).

## PR B — deterministic pipeline (`feat/candidate-engine-pipeline`)

| CE | What landed | Where | Verified by |
|---|---|---|---|
| CE-030 | Interval, containment, source-bound, duration and grounding validation; every applicable reason accumulated in canonical order | `domain/candidate_rules.py` | `tests/unit/test_candidate_validation.py` |
| CE-031 | Boundary snapping with a total order and whole-adjustment reversion | `domain/candidate_rules.py` | `tests/unit/test_boundary_snapping.py` |
| CE-032 | Interval IoU and greedy global deduplication after the score filter | `domain/candidate_rules.py` | `tests/unit/test_candidate_selection.py` |
| CE-033 | Global ranking and the `max_candidates` ceiling applied once | `domain/candidate_rules.py` | `tests/unit/test_candidate_selection.py` |
| — | Analysis stage identity: effective configuration and fingerprint, one canonical payload each | `domain/analysis_rules.py` | `tests/unit/test_analysis_service.py` |
| — | Orchestration, the four artifacts, and reuse verification | `services/analysis_service.py` | `tests/unit/test_analysis_service.py` |
| — | Fixture analyzer behind the port; no network, no SDK, no credential | `adapters/analysis/fixture_analyzer.py` | `tests/unit/test_fixture_analyzer.py` |
| — | `analyze RUN_ID --fixture PATH [--config PATH] [--force]` | `cli.py` | `tests/unit/test_cli_analyze.py` |

Decisions recorded: ADR-022 (the pipeline's binding rules, including the
grounding contract and every tie-break), ADR-023 (the fixture analyzer is the
executor until the provider exists).

Artifacts produced under `RUN_ID/analysis/`:

```text
chunks.json             ChunkCollection, versioned, tied to the transcript digest
candidates.raw.json     every proposal before a rule ran, with the verbatim responses
config.effective.json   what the stage really ran, including every rule version
candidates.json         the validated funnel: selected, rejected, invalid, events
```

## PR C — provider (not started)

| CE | What it is |
|---|---|
| CE-026 | The `clip_candidates/v1` prompt and its identity |
| CE-028 | The Gemini adapter behind `ContentAnalyzerPort` (ADR-019) |
| CE-029 | Structured-output parsing and provider-side validation |

Until then `manifest.versions.prompt_version` and `prompt_sha256` stay null: the
fields belong to CE-026, and the fixture's own identity is recorded in the stage
configuration instead, where it is true.

## What is still unmeasured

The pipeline has never seen real model output. Every proposal it has processed
was written by hand into a fixture, so the deterministic half is verified and
the quality of what it ranks is not. That measurement is the point of PR C.
