"""Identity of the analysis stage: what ran, and whether it may be reused.

The transcription stage already established the shape this follows (ADR-017).
Two digests, one canonical payload each, and one readable artifact beside the
output they describe:

``stage_config_sha256``   the digest of ``analysis/config.effective.json``. Ties
                          the manifest to a file a human can open, so the run
                          explains itself without anyone reversing a hash.
``analysis_fingerprint``  the digest of the whole stage: what it consumed and
                          what it produced. Decides whether the candidates on
                          disk may be reused.

The second one deliberately departs from the transcription stage, where the
fingerprint covers inputs only. Transcription writes one artifact the reuse
check reads back and validates in full; analysis writes four, and a digest over
inputs alone cannot tell that one of the outputs was edited. Since the decision
this digest makes is "may these four files be reused", it has to cover the four
files. It is an integrity digest over one execution, not a portable identity for
an experiment — ``config_sha256`` and ``stage_config_sha256`` remain the
portable ones.

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
    CandidateCollection,
    ChunkCollection,
    RawCandidateCollection,
)
from content_engine.domain.scoring import SCORE_FORMULA_VERSION
from content_engine.utils.canonical import canonical_sha256

#: Bumped whenever the fingerprint payload changes shape. Every recorded
#: fingerprint stops matching when it does, which is the intended effect: a
#: build that computes identity differently must not reuse artifacts produced by
#: one that computed it another way.
#:
#: 2: the payload covers all four artifacts rather than the raw batches alone.
ANALYSIS_FINGERPRINT_VERSION = 2

#: Named here rather than imported from the service, which imports this module.
CHUNKS_FILENAME_HINT = "chunks.json"


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
    #: Whether ``prompt`` names a packaged, versioned prompt resource that was
    #: really sent to a provider, or a stand-in an executor invented for itself.
    #:
    #: Both go into the stage configuration, where the field means "the prompt
    #: identity of whatever ran" and a fixture's ``fake-fixture/v1`` is a
    #: truthful answer. The manifest's ``prompt_version``/``prompt_sha256`` mean
    #: something narrower -- which versioned prompt this run sent -- and a
    #: fixture sent none. This flag is what keeps the two from being conflated,
    #: rather than inferring it from ``fixture_sha256`` being null, which would
    #: quietly become wrong the day a second non-provider executor exists.
    uses_packaged_prompt: bool = False


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


def analysis_fingerprint(
    transcript_sha256: str,
    chunks: ChunkCollection,
    raw: RawCandidateCollection,
    collection: CandidateCollection,
    config: AnalysisStageConfig,
) -> str:
    """The integrity of one analysis execution, artifacts included.

    Five parts, each of which decides something a later stage will act on. The
    transcript digest is the input the chunks were cut from. The chunks are the
    exact question the analyzer was asked. The raw collection is its answer,
    including the identity fields above the batches, because those say which
    build interpreted them. The validated collection is the shortlist a human
    will be shown. The stage configuration is everything else.

    The whole model is hashed in each case, not a chosen subset. Choosing a
    subset is how the first version of this left ``candidates.json`` and the raw
    metadata unprotected: every field left out is a field a later run trusts
    without evidence, and choosing correctly requires knowing in advance which
    edits matter.

    ``config_sha256`` remains the portable identity of an experiment. This is not
    portable and is not meant to be: two runs of identical inputs produce
    different fingerprints, because ``generated_at`` differs and the artifacts
    therefore differ.
    """
    return canonical_sha256(
        {
            "version": ANALYSIS_FINGERPRINT_VERSION,
            "transcript_sha256": transcript_sha256,
            "chunks": chunks.model_dump(mode="json"),
            "raw": raw.model_dump(mode="json"),
            "candidates": collection.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        }
    )


def coherence_problem(
    transcript_sha256: str,
    chunks: ChunkCollection,
    raw: RawCandidateCollection,
    collection: CandidateCollection,
    config: AnalysisStageConfig,
) -> str | None:
    """The first way these four artifacts contradict each other, or None.

    The fingerprint proves the four files are the ones that were written
    together. It cannot prove they were coherent when they were written, and it
    cannot prove they still describe the transcript in front of us — a set of
    artifacts moved from another run would be internally consistent and
    completely wrong.

    Returns a description rather than raising, because the caller decides what
    kind of failure this is: producing an incoherent set is an analysis bug,
    finding one on disk is an incompatible artifact.
    """
    if chunks.transcript_sha256 != transcript_sha256:
        return (
            f"{CHUNKS_FILENAME_HINT} was cut from transcript "
            f"{chunks.transcript_sha256[:12]}, but the run holds {transcript_sha256[:12]}"
        )
    if raw.transcript_sha256 != chunks.transcript_sha256:
        return (
            f"the raw candidates name transcript {raw.transcript_sha256[:12]} and the "
            f"chunks name {chunks.transcript_sha256[:12]}"
        )

    executor: tuple[tuple[str, object, object], ...] = (
        ("analyzer", raw.analyzer, config.analyzer),
        ("analyzer_version", raw.analyzer_version, config.analyzer_version),
        ("model", raw.model, config.model),
        ("prompt_version", raw.prompt_version, config.prompt_version),
        ("prompt_sha256", raw.prompt_sha256, config.prompt_sha256),
        ("fixture_sha256", raw.fixture_sha256, config.fixture_sha256),
    )
    for field, recorded, configured in executor:
        if recorded != configured:
            return (
                f"the raw candidates and the stage configuration disagree on {field}: "
                f"{recorded!r} against {configured!r}"
            )

    answered = [batch.chunk_id for batch in raw.batches]
    expected = [chunk.id for chunk in chunks.chunks]
    if sorted(answered) != sorted(expected):
        return f"the raw candidates answer {sorted(answered)} but the chunks are {sorted(expected)}"

    if raw.proposed_count != collection.counts.proposed:
        return (
            f"the raw candidates hold {raw.proposed_count} proposals and the funnel "
            f"counts {collection.counts.proposed}"
        )

    if collection.source_duration_seconds != chunks.source_duration_seconds:
        return (
            f"the candidates describe {collection.source_duration_seconds} seconds of "
            f"source and the chunks {chunks.source_duration_seconds}"
        )
    # Every one of the four records the chunking rules version, so every one of
    # them has to agree. Comparing a pair left the other two free to claim the
    # windows were cut by rules that produced something else, and the version is
    # what says whether the analyzer was shown the same material at all.
    declared = (
        ("the chunks", chunks.rules_version),
        ("the raw candidates", raw.rules_version),
        ("the candidates", collection.rules_version),
        ("the stage configuration", config.chunking_rules_version),
    )
    if len({version for _, version in declared}) > 1:
        described = ", ".join(f"{name} {version}" for name, version in declared)
        return f"the artifacts name different chunking rules: {described}"

    policy: tuple[tuple[str, object, object], ...] = (
        ("score_formula_version", collection.score_formula_version, config.score_formula_version),
        ("target_candidates", collection.target_candidates, config.target_candidates),
        ("max_candidates", collection.max_candidates, config.max_candidates),
        ("min_score", collection.min_score, config.min_score),
        ("dedupe_iou", collection.dedupe_iou, config.dedupe_iou),
        (
            "boundary_snap_seconds",
            collection.boundary_snap_seconds,
            config.boundary_snap_seconds,
        ),
    )
    for field, recorded, configured in policy:
        if recorded != configured:
            return (
                f"the candidates and the stage configuration disagree on {field}: "
                f"{recorded!r} against {configured!r}"
            )
    return None
