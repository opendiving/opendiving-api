"""Sampling bounds output without changing channel alignment or full-series extrema."""

from typing import Any

from src.app.mcp.profiles import sampled_profile


def test_profile_sampling_preserves_alignment_and_original_extrema() -> None:
    times = list(range(1001))
    readings = [float(index % 83) for index in times]
    readings[503] = -7.0
    readings[507] = 120.0
    original: dict[str, Any] = {"depth": {"times": times, "values": readings}, "unit": "m"}
    output = sampled_profile(original, 10)
    channel = output["profile"]["depth"]
    assert len(channel["times"]) == len(channel["values"]) == 10
    assert channel["times"][0] == 0
    assert channel["times"][-1] == 1000
    assert channel["values"] == [readings[index] for index in channel["times"]]
    assert output["series"]["depth"] == {"original_points": 1001, "minimum": -7.0, "maximum": 120.0}
    assert original["depth"]["times"] == times
    assert output["profile"]["unit"] == "m"
