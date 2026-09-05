from content_engine.utils.timestamps import srt_timestamp


def test_srt_timestamp_formats_seconds() -> None:
    assert srt_timestamp(782.42) == "00:13:02,420"


def test_srt_timestamp_clamps_negative_values() -> None:
    assert srt_timestamp(-1) == "00:00:00,000"
