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

## PR B — deterministic pipeline (merged, `1b15e0f`, PR #5)

| CE | What landed | Where | Verified by |
|---|---|---|---|
| CE-030 | Interval, containment, source-bound, duration and grounding validation; every applicable reason accumulated in canonical order | `domain/candidate_rules.py` | `tests/unit/test_candidate_validation.py` |
| CE-031 | Boundary snapping with a total order and whole-adjustment reversion | `domain/candidate_rules.py` | `tests/unit/test_boundary_snapping.py` |
| CE-032 | Interval IoU and greedy global deduplication after the score filter | `domain/candidate_rules.py` | `tests/unit/test_candidate_selection.py` |
| CE-033 | Global ranking and the `max_candidates` ceiling applied once | `domain/candidate_rules.py` | `tests/unit/test_candidate_selection.py` |
| — | Analysis stage identity: effective configuration and fingerprint, one canonical payload each | `domain/analysis_rules.py` | `tests/unit/test_analysis_service.py` |
| — | Orchestration, the four artifacts, and reuse verification | `services/analysis_service.py` | `tests/unit/test_analysis_service.py` |
| — | Reuse proves all four artifacts and their coherence; a verified reuse recovers a failed run | `services/analysis_service.py`, `domain/analysis_rules.py`, `cli.py` | `tests/unit/test_analysis_reuse.py`, `tests/unit/test_cli_analyze.py` |
| — | Fixture analyzer behind the port; no network, no SDK, no credential | `adapters/analysis/fixture_analyzer.py` | `tests/unit/test_fixture_analyzer.py` |
| — | `analyze RUN_ID --fixture PATH [--config PATH] [--force]` | `cli.py` | `tests/unit/test_cli_analyze.py` |

Decisions recorded: ADR-022 (the pipeline's binding rules, including the
grounding contract and every tie-break), ADR-023 (the fixture analyzer is the
executor until the provider exists), ADR-024 (the analysis fingerprint covers
the artifacts, not only the inputs — written after an independent review found
that two of the four were being reused without evidence).

Artifacts produced under `RUN_ID/analysis/`. All four are covered by the
fingerprint and all four are read back and validated before a reuse:

```text
chunks.json             ChunkCollection, versioned, tied to the transcript digest
candidates.raw.json     every proposal before a rule ran, with the verbatim responses
config.effective.json   what the stage really ran, including every rule version
candidates.json         the validated funnel: selected, rejected, invalid, events
```

`analysis/raw/` is created by `RunWorkspace.create` and left empty: the spec's
per-chunk `raw/chunk_*.json` files are not written, and `candidates.raw.json` is
the canonical aggregate.

## PR C — provider (`feat/gemini-candidate-provider`)

| CE | What landed | Where | Verified by |
|---|---|---|---|
| CE-026 | The `clip_candidates/v1` prompt: packaged resource, versioned, hashed over line-ending-normalised text | `resources/prompts/clip_candidates/v1.txt`, `adapters/analysis/prompt.py` | `tests/unit/test_clip_candidates_prompt.py` |
| CE-028 | `GeminiContentAnalyzer` behind the port; system instruction separate from speech, no tools, bounded retries, sanitised errors | `adapters/analysis/gemini_analyzer.py` | `tests/unit/test_gemini_analyzer.py` |
| CE-029 | Structured output: the schema sent to the provider and the strict parse of what returns | `adapters/analysis/structured_output.py` | `tests/unit/test_gemini_structured_output.py` |
| — | `analyze RUN_ID [--fixture PATH] [--config PATH] [--force]`, two modes that never share artifacts | `cli.py` | `tests/unit/test_cli_analyze_provider.py` |
| — | Exactly one file in the package may import a provider SDK or a network client | `ports/analyzer.py`, `adapters/` | AST sweeps in `test_analyzer_port.py`, `test_fixture_analyzer.py` |
| — | One opt-in test that really calls Gemini, off unless explicitly enabled | `tests/ai/test_gemini_live.py` | skipped unless `CONTENT_ENGINE_RUN_AI_TESTS=1` |

Decisions recorded: ADR-025 (the prompt is a packaged resource, not a repository
file), ADR-026 (`google-genai` is a runtime dependency rather than an optional
extra), ADR-027 (retries, exit codes, and the fact that an unparseable response
is not persisted).

`manifest.versions.prompt_version` and `prompt_sha256` remain null for a fixture
run and are set from the selected prompt for a provider run. Switching executor
with `--force` rewrites them in both directions, so they never describe the
previous run. The stage configuration separately records what ran and what the
configuration asked for, so the two are distinguishable rather than inferred.

`analysis.prompt_version` selects the prompt. It names a short version (`v1`)
which resolves to a packaged resource and to the qualified identity
(`clip_candidates/v1`) recorded in artifacts. An unknown version is refused with
exit 2 before the run is touched, in both fixture and provider mode.

## What is still unmeasured

The deterministic half is verified against hand-written proposals, and the
adapter is verified against doubles. Neither is a measurement of candidate
quality: that requires real model output over real videos, and the evaluation
the specification asks for needs at least five representative sources. A single
video can demonstrate that the integration works and give a first signal. It
cannot show that the engine picks good clips.
