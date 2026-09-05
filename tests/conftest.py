from __future__ import annotations

from pathlib import Path

import pytest

from content_engine.config import WORKSPACE_ENV_VAR, Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Canonical settings with the workspace redirected into the test directory."""
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("workspace")))
    monkeypatch.delenv("CONTENT_ENGINE_ANALYSIS_MODEL", raising=False)
    return load_settings()
