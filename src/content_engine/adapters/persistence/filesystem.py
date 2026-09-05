from __future__ import annotations

from pathlib import Path

from content_engine.domain.exceptions import InvalidMediaError
from content_engine.domain.models import RunManifest
from content_engine.utils.json import read_json, write_json


class RunWorkspace:
    DIRECTORIES = (
        "media",
        "audio",
        "transcript",
        "analysis",
        "review",
        "previews",
        "clips",
        "logs",
    )

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def runs_root(self) -> Path:
        return self.root.joinpath("runs")

    def run_path(self, run_id: str) -> Path:
        candidate = Path(run_id)
        if (
            not run_id
            or run_id in {".", ".."}
            or candidate.name != run_id
            or len(candidate.parts) != 1
        ):
            raise InvalidMediaError(f"Invalid run identifier: {run_id}")
        return self.runs_root.joinpath(run_id)

    def create(self, run_id: str) -> Path:
        run_path = self.run_path(run_id)
        run_path.mkdir(parents=True, exist_ok=False)
        for directory in self.DIRECTORIES:
            run_path.joinpath(directory).mkdir()
        run_path.joinpath("analysis", "raw").mkdir()
        return run_path

    def require(self, run_id: str) -> Path:
        path = self.run_path(run_id)
        if not path.is_dir():
            raise InvalidMediaError(f"Run does not exist: {run_id}")
        return path

    def write_manifest(self, run_path: Path, manifest: RunManifest) -> None:
        write_json(run_path.joinpath("manifest.json"), manifest.model_dump(mode="json"))

    def read_manifest(self, run_path: Path) -> RunManifest:
        return RunManifest.model_validate(read_json(run_path.joinpath("manifest.json")))
