from __future__ import annotations

from pathlib import Path

import pytest

from content_engine.config import load_settings
from content_engine.domain.exceptions import ConfigurationError


def _profile(tmp_path: Path, body: str) -> Path:
    profile = tmp_path.joinpath("profile.toml")
    profile.write_text(body, encoding="utf-8")
    return profile


def test_unknown_key_is_rejected_and_named(tmp_path: Path) -> None:
    profile = _profile(tmp_path, '[transcription]\nmodle = "tiny"\n')

    with pytest.raises(ConfigurationError) as error:
        load_settings(profile)

    assert "transcription.modle" in str(error.value)


def test_unknown_section_is_rejected(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "[nonexistent]\nfoo = 1\n")

    with pytest.raises(ConfigurationError, match="nonexistent"):
        load_settings(profile)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "[analysis.candidates]\nmin_duration_seconds = 90\nmax_duration_seconds = 20\n",
            "min_duration_seconds",
        ),
        (
            "[analysis.candidates]\ntarget_candidates = 50\nmax_candidates = 5\n",
            "target_candidates",
        ),
        (
            "[analysis.chunking]\nwindow_seconds = 10\noverlap_seconds = 600\n",
            "overlap_seconds",
        ),
        ("[render]\nwidth = 1081\n", "render.width"),
        ("[preview]\nheight = 961\n", "preview.height"),
    ],
)
def test_cross_field_invariants_name_the_broken_relation(
    tmp_path: Path, body: str, expected: str
) -> None:
    profile = _profile(tmp_path, body)

    with pytest.raises(ConfigurationError) as error:
        load_settings(profile)

    assert expected in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        '[transcription]\nprovider = "not-a-provider"\n',
        '[transcription]\ndevice = "banana"\n',
        '[transcription]\ncompute_type = "nonsense"\n',
        '[analysis]\nprovider = "not-a-provider"\n',
        '[render]\npreset = "totally_invalid"\n',
    ],
)
def test_closed_value_sets_are_enforced(tmp_path: Path, body: str) -> None:
    profile = _profile(tmp_path, body)

    with pytest.raises(ConfigurationError):
        load_settings(profile)


@pytest.mark.parametrize(
    "body",
    [
        "[transcription]\nbeam_size = 0\n",
        "[render]\ncrf = 52\n",
        "[analysis.candidates]\ndedupe_iou = 1.5\n",
        "[analysis.candidates]\nboundary_snap_seconds = 0\n",
        '[transcription]\nmodel = ""\n',
    ],
)
def test_field_ranges_are_enforced(tmp_path: Path, body: str) -> None:
    profile = _profile(tmp_path, body)

    with pytest.raises(ConfigurationError):
        load_settings(profile)


def test_boundary_snap_seconds_is_configurable(tmp_path: Path) -> None:
    profile = _profile(tmp_path, "[analysis.candidates]\nboundary_snap_seconds = 1.5\n")

    assert load_settings(profile).analysis.candidates.boundary_snap_seconds == 1.5
