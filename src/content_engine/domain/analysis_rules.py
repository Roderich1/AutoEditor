"""Identity of the analysis stage: what ran, and whether it may be reused.

The transcription stage already established the shape this follows (ADR-017).
Two digests, one canonical payload each, and one readable artifact beside the
output they describe:

``stage_config_sha256``   the digest of ``analysis/config.effective.json``. Ties
                          the manifest to a file a human can open, so the run
                          explains itself without anyone reversing a hash.
``analysis_fingerprint``  the digest of everything the stage consumed. Decides
                          whether the candidates on disk may be reused.

There is one payload builder for each, used both when writing the stage and when
verifying it later. Two implementations of "the same payload" agree exactly
until the day one of them is edited, and the failure then is silent reuse of
artifacts that no longer match their inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

from content_engine.domain.candidate_rules import (
    BOUNDARY_RULES_VERSION,
    CANDIDATE_ID_VERSION,
    DEDUPE_RULES_VERSION,
    RANKING_RULES_VERSION,
    VALIDATION_RULES_VERSION,
    CandidatePolicy,
    PromptIdentity,
)
from content_engine.domain.candidates import (
    CANDIDATES_SCHEMA_VERSION,
    CHUNKING_RULES_VERSION,
    CHUNKS_SCHEMA_VERSION,
    RAW_CANDIDATES_SCHEMA_VERSION,
    AnalysisStageConfig,
    RawCandidateCollection,
)
from content_engine.domain.scoring import SCORE_FORMULA_VERSION
from content_engine.utils.canonical import canonical_sha256

#: Bumped whenever the fingerprint payload changes shape. Every recorded
#: fingerprint stops matching when it does, which is the intended effect: a
#: build that computes identity differently must not reuse artifacts produced by
#: one that computed it another way.
ANALYSIS_FINGERPRINT_VERSION = 1


@dataclass(frozen=True)
class AnalyzerIdentity:
    """Who actually produced the candidates, resolved at run time.

    Separate from the configured provider on purpose. ``analysis.provider`` says
    which adapter the configuration would use; this says which one ran. In this
    pull request they differ on every single run — the configuration names
    Gemini and a fixture replays the answers — and a manifest that recorded only
    the former would assert an external call that never happened.
    """

    analyzer: str
    analyzer_version: str
    model: str
    prompt: PromptIdentity
    #: The fixture these answers were replayed from. Null for a real provider,
    #: which is the only truthful value once one exists.
    fixture_sha256: str | None = None


def analysis_stage_config(
    identity: AnalyzerIdentity,
    provider_configured: str,
    model_configured: str,
    window_seconds: int,
    overlap_seconds: int,
    policy: CandidatePolicy,
) -> AnalysisStageConfig:
    """Everything that decides what this stage produces, in readable form.

    Every rule version is in here rather than only in the fingerprint. A change
    to snapping or to deduplication changes the shortlist without changing a
    single setting, so a run that could not name the rules it used would be
    unreproducible in exactly the way that is hardest to notice.
    """
    return AnalysisStageConfig(
        analyzer=identity.analyzer,
        analyzer_version=identity.analyzer_version,
        model=identity.model,
        provider_configured=provider_configured,
        model_configured=model_configured,
        prompt_version=identity.prompt.version,
        prompt_sha256=identity.prompt.sha256,
        fixture_sha256=identity.fixture_sha256,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
        min_duration_seconds=policy.min_duration_seconds,
        max_duration_seconds=policy.max_duration_seconds,
        min_score=policy.min_score,
        target_candidates=policy.target_candidates,
        max_candidates=policy.max_candidates,
        dedupe_iou=policy.dedupe_iou,
        boundary_snap_seconds=policy.boundary_snap_seconds,
        chunking_rules_version=CHUNKING_RULES_VERSION,
        validation_rules_version=VALIDATION_RULES_VERSION,
        boundary_rules_version=BOUNDARY_RULES_VERSION,
        dedupe_rules_version=DEDUPE_RULES_VERSION,
        ranking_rules_version=RANKING_RULES_VERSION,
        candidate_id_version=CANDIDATE_ID_VERSION,
        score_formula_version=SCORE_FORMULA_VERSION,
        chunks_schema_version=CHUNKS_SCHEMA_VERSION,
        raw_schema_version=RAW_CANDIDATES_SCHEMA_VERSION,
        candidates_schema_version=CANDIDATES_SCHEMA_VERSION,
    )


def stage_config_sha256(config: AnalysisStageConfig) -> str:
    """Digest of the stage configuration exactly as it is written to disk."""
    return canonical_sha256(config.model_dump(mode="json"))


def raw_batches_sha256(raw: RawCandidateCollection) -> str:
    """Digest of what the analyzer returned, verbatim responses included.

    Covers the batches only, not the identity fields around them: those are
    already in the stage configuration, and hashing them twice would make the
    fingerprint change for two different reasons that are impossible to tell
    apart when it does.
    """
    return canonical_sha256({"batches": [batch.model_dump(mode="json") for batch in raw.batches]})


def analysis_fingerprint(
    transcript_sha256: str,
    raw_batches_digest: str,
    config: AnalysisStageConfig,
) -> str:
    """The identity of one analysis execution.

    Three parts, and each is load-bearing. The transcript digest is the input
    the chunks were cut from. The batch digest is what the analyzer answered,
    which for a fixture run is the fixture and for a provider run is a
    non-reproducible response that must still be pinned to the artifacts derived
    from it. The stage configuration is everything else: analyzer identity,
    prompt identity, fixture digest, chunking, the candidate policy, every rule
    version and every schema version.

    Deliberately not portable, in the sense of ADR-017: it identifies stage
    inputs on this run, not a logical experiment. ``config_sha256`` is the
    portable one.
    """
    return canonical_sha256(
        {
            "version": ANALYSIS_FINGERPRINT_VERSION,
            "transcript_sha256": transcript_sha256,
            "raw_batches_sha256": raw_batches_digest,
            "config": config.model_dump(mode="json"),
        }
    )
