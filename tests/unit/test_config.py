from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from content_engine.config import (
    ANALYSIS_MODEL_ENV_VAR,
    ENVIRONMENT_ONLY_FIELDS,
    WORKSPACE_ENV_VAR,
    Settings,
    canonical_config,
    config_sha256,
    config_sources,
    load_settings,
    read_packaged_defaults,
    resolve_workspace_root,
)
from content_engine.domain.exceptions import ConfigurationError


def _fields_without_defaults(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Every settings field must be required so defaults live only in the TOML."""
    offenders: list[str] = []
    for name, field in model.model_fields.items():
        location = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            offenders.extend(_fields_without_defaults(annotation, f"{location}."))
        elif not field.is_required():
            offenders.append(location)
    return offenders


def test_defaults_live_only_in_the_packaged_toml() -> None:
    assert _fields_without_defaults(Settings) == []


def test_packaged_defaults_populate_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKSPACE_ENV_VAR, raising=False)
    monkeypatch.delenv(ANALYSIS_MODEL_ENV_VAR, raising=False)

    settings = Settings.model_validate(read_packaged_defaults())

    assert settings.transcription.model == "large-v3"
    assert settings.analysis.candidates.boundary_snap_seconds == 2.5


def test_configuration_loads_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(WORKSPACE_ENV_VAR, raising=False)

    settings = load_settings()

    assert settings.transcription.model == "large-v3"
    assert settings.workspace.root == tmp_path.joinpath("workspace").resolve()


def test_profile_is_merged_over_packaged_defaults(tmp_path: Path) -> None:
    profile = tmp_path.joinpath("profile.toml")
    profile.write_text('[transcription]\nmodel = "tiny"\n', encoding="utf-8")

    settings = load_settings(profile)

    assert settings.transcription.model == "tiny"
    assert settings.transcription.beam_size == 5
    assert settings.workspace.root.is_absolute()


#: Anchored to this file, never to the working directory: the suite must pass
#: from anywhere, exactly as the CLI does.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("profile", ["fast.toml", "quality.toml"])
def test_shipped_profiles_remain_valid_overlays(profile: str) -> None:
    settings = load_settings(REPOSITORY_ROOT.joinpath("configs", profile))

    assert settings.transcription.model
    assert settings.analysis.candidates.boundary_snap_seconds == 2.5


def test_workspace_environment_variable_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("elsewhere")))

    settings = load_settings()

    assert settings.workspace.root == tmp_path.joinpath("elsewhere").resolve()
    assert f"env {WORKSPACE_ENV_VAR}" in config_sources()


def test_analysis_model_environment_variable_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANALYSIS_MODEL_ENV_VAR, "model-from-env")

    assert load_settings().analysis.model == "model-from-env"


def test_relative_workspace_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert resolve_workspace_root(Path("runs")) == tmp_path.joinpath("runs").resolve()


def test_missing_profile_reports_configuration_error(tmp_path: Path) -> None:
    absent = tmp_path.joinpath("absent.toml")

    with pytest.raises(ConfigurationError, match="Cannot load configuration"):
        load_settings(absent)


def test_logical_hash_ignores_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("machine-a")))
    first = config_sha256(load_settings())
    monkeypatch.setenv(WORKSPACE_ENV_VAR, str(tmp_path.joinpath("machine-b", "deeper")))
    second = config_sha256(load_settings())

    assert first == second
    assert ENVIRONMENT_ONLY_FIELDS == (("workspace", "root"),)


def test_logical_hash_tracks_experiment_relevant_changes(tmp_path: Path) -> None:
    baseline = config_sha256(load_settings())
    profile = tmp_path.joinpath("profile.toml")
    profile.write_text('[transcription]\nmodel = "tiny"\n', encoding="utf-8")

    assert config_sha256(load_settings(profile)) != baseline


def test_canonical_config_keeps_the_workspace_section_for_future_fields() -> None:
    canonical = canonical_config(load_settings())

    assert canonical["workspace"] == {}
    assert "render" in canonical
