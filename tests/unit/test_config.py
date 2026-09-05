from pathlib import Path

from content_engine.config import load_settings


def test_profile_is_merged_with_defaults(tmp_path: Path) -> None:
    profile = tmp_path.joinpath("profile.toml")
    profile.write_text('[transcription]\nmodel = "tiny"\n', encoding="utf-8")

    settings = load_settings(profile)

    assert settings.transcription.model == "tiny"
    assert settings.transcription.beam_size == 5
    assert settings.workspace.root.is_absolute()
