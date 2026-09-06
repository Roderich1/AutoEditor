"""Orchestration of the analysis stage.

Calls the analyzer once per chunk, runs the deterministic pipeline over
everything it returned, and writes the four artifacts that describe what
happened. It knows nothing about which analyzer it is holding: the port takes
domain types only, so a fixture and a future Gemini adapter are the same thing
from here.

Two properties are enforced by the shape of this module rather than by care.

**Nothing is written until everything is valid.** The chunks, the raw batches,
the validated collection and the stage configuration are all built and validated
in memory first. A stage that fails leaves no partial artifact behind that a
later run could mistake for a completed one, and the manifest is only advanced
by the caller once all four files exist.

**Reuse is proved, never assumed.** The verification path recomputes both digests
from what is actually on disk and rebuilds the fingerprint from the transcript
and the recorded batches. A digest that still looks right proves nothing if the
artifact it addresses was edited or replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from content_engine.config import Settings
from content_engine.domain.analysis_rules import (
    AnalyzerIdentity,
    analysis_fingerprint,
    analysis_stage_config,
    raw_batches_sha256,
    stage_config_sha256,
)
from content_engine.domain.candidate_rules import (
    CandidatePolicy,
    Proposal,
    candidate_id,
    select_candidates,
)
from content_engine.domain.candidates import (
    ANALYSIS_STAGE_CONFIG_SCHEMA_VERSION,
    CANDIDATES_SCHEMA_VERSION,
    CHUNKING_RULES_VERSION,
    RAW_CANDIDATES_SCHEMA_VERSION,
    AnalysisStageConfig,
    CandidateCollection,
    ChunkCollection,
    RawCandidateBatch,
    RawCandidateCollection,
)
from content_engine.domain.exceptions import (
    AnalysisError,
    IncompatibleArtifactError,
)
from content_engine.domain.models import Transcript
from content_engine.ports.analyzer import AnalysisContext, ContentAnalyzerPort
from content_engine.services.chunking_service import CHUNKS_FILENAME, build_chunk_collection
from content_engine.utils.json import read_json, write_json

RAW_CANDIDATES_FILENAME = "candidates.raw.json"
CANDIDATES_FILENAME = "candidates.json"
#: The stage's own effective configuration, beside the artifacts it produced.
STAGE_CONFIG_FILENAME = "config.effective.json"

#: Written in this order, and the order matters. The file the reuse check looks
#: for first is written last, so an interrupted run cannot leave a directory
#: that looks complete.
ARTIFACT_FILENAMES = (
    CHUNKS_FILENAME,
    RAW_CANDIDATES_FILENAME,
    STAGE_CONFIG_FILENAME,
    CANDIDATES_FILENAME,
)


def candidate_policy(settings: Settings) -> CandidatePolicy:
    candidates = settings.analysis.candidates
    return CandidatePolicy(
        min_duration_seconds=candidates.min_duration_seconds,
        max_duration_seconds=candidates.max_duration_seconds,
        min_score=candidates.min_score,
        target_candidates=candidates.target_candidates,
        max_candidates=candidates.max_candidates,
        dedupe_iou=candidates.dedupe_iou,
        boundary_snap_seconds=candidates.boundary_snap_seconds,
    )


@dataclass(frozen=True)
class AnalysisPlan:
    """Everything decided before the analyzer is called.

    Built separately so the caller can check the analyzer against the chunks it
    will be asked about, and so the reuse path can rebuild the configuration it
    expects without running anything.
    """

    chunks: ChunkCollection
    policy: CandidatePolicy
    stage_config: AnalysisStageConfig


@dataclass(frozen=True)
class AnalysisOutcome:
    chunks: ChunkCollection
    raw: RawCandidateCollection
    collection: CandidateCollection
    stage_config: AnalysisStageConfig
    stage_config_sha256: str
    fingerprint: str


def plan_analysis(
    transcript: Transcript, settings: Settings, identity: AnalyzerIdentity
) -> AnalysisPlan:
    chunking = settings.analysis.chunking
    policy = candidate_policy(settings)
    return AnalysisPlan(
        chunks=build_chunk_collection(transcript, chunking),
        policy=policy,
        stage_config=analysis_stage_config(
            identity,
            provider_configured=str(settings.analysis.provider),
            model_configured=settings.analysis.model,
            window_seconds=chunking.window_seconds,
            overlap_seconds=chunking.overlap_seconds,
            policy=policy,
        ),
    )


class AnalysisService:
    def __init__(self, analyzer: ContentAnalyzerPort, identity: AnalyzerIdentity) -> None:
        self.analyzer = analyzer
        self.identity = identity

    def analyze(
        self,
        plan: AnalysisPlan,
        output_directory: Path,
        generated_at: datetime,
    ) -> AnalysisOutcome:
        context = AnalysisContext(
            min_duration_seconds=plan.policy.min_duration_seconds,
            max_duration_seconds=plan.policy.max_duration_seconds,
            run_target_candidates=plan.policy.target_candidates,
            prompt_version=self.identity.prompt.version,
            prompt_sha256=self.identity.prompt.sha256,
        )

        batches: list[RawCandidateBatch] = []
        proposals: list[Proposal] = []
        for chunk in plan.chunks.chunks:
            batch = self.analyzer.find_candidates(chunk, context)
            # An analyzer that answers about a different chunk than the one it
            # was asked about would attach candidates to the wrong material, and
            # every timestamp check downstream would be validating them against
            # a window they did not come from.
            if batch.chunk_id != chunk.id:
                raise AnalysisError(
                    f"The analyzer was asked about {chunk.id} and answered about {batch.chunk_id}."
                )
            batches.append(
                RawCandidateBatch(
                    chunk_id=batch.chunk_id,
                    candidates=list(batch.candidates),
                    raw_response=batch.raw_response,
                    model=batch.model,
                )
            )
            proposals.extend(
                Proposal(
                    id=candidate_id(
                        plan.chunks.transcript_sha256,
                        chunk.id,
                        ordinal,
                        raw,
                        self.identity.prompt,
                    ),
                    chunk=chunk,
                    ordinal=ordinal,
                    raw=raw,
                )
                for ordinal, raw in enumerate(batch.candidates)
            )

        raw = RawCandidateCollection(
            schema_version=RAW_CANDIDATES_SCHEMA_VERSION,
            rules_version=CHUNKING_RULES_VERSION,
            transcript_sha256=plan.chunks.transcript_sha256,
            analyzer=self.identity.analyzer,
            analyzer_version=self.identity.analyzer_version,
            model=self.identity.model,
            prompt_version=self.identity.prompt.version,
            prompt_sha256=self.identity.prompt.sha256,
            fixture_sha256=self.identity.fixture_sha256,
            batches=batches,
            proposed_count=len(proposals),
        )
        collection = select_candidates(
            proposals,
            plan.chunks.source_duration_seconds,
            plan.policy,
            CHUNKING_RULES_VERSION,
            generated_at,
        )
        digest = stage_config_sha256(plan.stage_config)
        fingerprint = analysis_fingerprint(
            plan.chunks.transcript_sha256, raw_batches_sha256(raw), plan.stage_config
        )

        output_directory.mkdir(parents=True, exist_ok=True)
        write_json(output_directory.joinpath(CHUNKS_FILENAME), plan.chunks.model_dump(mode="json"))
        write_json(output_directory.joinpath(RAW_CANDIDATES_FILENAME), raw.model_dump(mode="json"))
        write_json(
            output_directory.joinpath(STAGE_CONFIG_FILENAME),
            plan.stage_config.model_dump(mode="json"),
        )
        write_json(
            output_directory.joinpath(CANDIDATES_FILENAME), collection.model_dump(mode="json")
        )
        return AnalysisOutcome(
            chunks=plan.chunks,
            raw=raw,
            collection=collection,
            stage_config=plan.stage_config,
            stage_config_sha256=digest,
            fingerprint=fingerprint,
        )


def _load(path: Path, description: str) -> dict[str, object]:
    if not path.is_file():
        raise IncompatibleArtifactError(
            f"Candidates exist but {path.name} is missing from {path.parent}, so there is "
            f"no record of {description}. Rerun with --force."
        )
    try:
        payload = read_json(path)
    except Exception as error:  # noqa: BLE001 - every read failure is one refusal
        raise IncompatibleArtifactError(
            f"{path} cannot be read as {description}: {error}. Rerun with --force."
        ) from error
    if not isinstance(payload, dict):
        raise IncompatibleArtifactError(
            f"{path} does not contain {description}. Rerun with --force."
        )
    return payload


def read_stage_config(directory: Path) -> AnalysisStageConfig:
    """Load the configuration the analysis stage recorded, or refuse it."""
    path = directory.joinpath(STAGE_CONFIG_FILENAME)
    payload = _load(path, "the configuration of the analysis stage")
    declared = payload.get("schema_version")
    if declared != ANALYSIS_STAGE_CONFIG_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares stage configuration schema {declared!r}; this build "
            f"understands {ANALYSIS_STAGE_CONFIG_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return AnalysisStageConfig.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid analysis stage configuration: {error}. Rerun with --force."
        ) from error


def read_raw_collection(directory: Path) -> RawCandidateCollection:
    path = directory.joinpath(RAW_CANDIDATES_FILENAME)
    payload = _load(path, "the raw candidates the analyzer returned")
    declared = payload.get("schema_version")
    if declared != RAW_CANDIDATES_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares raw candidate schema {declared!r}; this build understands "
            f"{RAW_CANDIDATES_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return RawCandidateCollection.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid raw candidate collection: {error}. Rerun with --force."
        ) from error


def read_candidates(directory: Path) -> CandidateCollection:
    path = directory.joinpath(CANDIDATES_FILENAME)
    payload = _load(path, "the validated candidates")
    declared = payload.get("schema_version")
    if declared != CANDIDATES_SCHEMA_VERSION:
        raise IncompatibleArtifactError(
            f"{path} declares candidate schema {declared!r}; this build understands "
            f"{CANDIDATES_SCHEMA_VERSION}. Rerun with --force."
        )
    try:
        return CandidateCollection.model_validate(payload)
    except ValidationError as error:
        raise IncompatibleArtifactError(
            f"{path} is not a valid candidate collection: {error}. Rerun with --force."
        ) from error


def verify_analysis(
    directory: Path,
    recorded_fingerprint: str,
    recorded_stage_config_sha256: str,
    transcript_sha256: str,
    expected: AnalysisStageConfig,
) -> CandidateCollection:
    """Prove the artifacts on disk still describe the run being asked for.

    Four separate claims, checked in the order that gives the most specific
    message first:

    1. the stage configuration on disk is the one the manifest recorded;
    2. the raw batches are readable and the fingerprint rebuilds from them, the
       transcript and that configuration;
    3. the configuration the caller is asking for now is that same one, which is
       what catches a different fixture, a different profile or a rule version
       this build changed;
    4. the validated candidates themselves are readable and still satisfy every
       invariant of the collection.

    Nothing is written. Every refusal leaves all four artifacts and the manifest
    exactly as they were.
    """
    config = read_stage_config(directory)
    recomputed = stage_config_sha256(config)
    if recomputed != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"{directory.joinpath(STAGE_CONFIG_FILENAME)} does not match the manifest "
            f"(recorded {recorded_stage_config_sha256[:12]}, recomputed {recomputed[:12]}). "
            "The stage configuration was changed after the candidates were produced. "
            "Rerun with --force."
        )

    raw = read_raw_collection(directory)
    rebuilt = analysis_fingerprint(transcript_sha256, raw_batches_sha256(raw), config)
    if rebuilt != recorded_fingerprint:
        raise IncompatibleArtifactError(
            f"The recorded fingerprint cannot be rebuilt from the transcript, "
            f"{RAW_CANDIDATES_FILENAME} and {STAGE_CONFIG_FILENAME} (recorded "
            f"{recorded_fingerprint[:12]}, rebuilt {rebuilt[:12]}). The candidates, their "
            "inputs and the manifest no longer describe one execution. Rerun with --force."
        )

    wanted = stage_config_sha256(expected)
    if wanted != recorded_stage_config_sha256:
        raise IncompatibleArtifactError(
            f"The existing candidates were produced under different settings "
            f"(recorded {recorded_stage_config_sha256[:12]}, current {wanted[:12]}). The "
            "analyzer, the fixture, the candidate policy or a rule version differs. "
            "They will not be reused. Rerun with --force."
        )
    return read_candidates(directory)
