"""Bound profile output while retaining extrema from every original sample."""

from typing import Any


def sampled_profile(profile: dict[str, Any], max_points: int) -> dict[str, Any]:
    summaries: dict[str, Any] = {}

    def sample(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            times, readings = value.get("times"), value.get("values")
            if isinstance(times, list) and isinstance(readings, list) and len(times) == len(readings):
                count = len(times)
                summaries[path] = {
                    "original_points": count,
                    "minimum": min(readings) if readings else None,
                    "maximum": max(readings) if readings else None,
                }
                if count > max_points:
                    indices = [index * (count - 1) // (max_points - 1) for index in range(max_points)]
                    return {
                        **value,
                        "times": [times[index] for index in indices],
                        "values": [readings[index] for index in indices],
                    }
                return value
            return {key: sample(child, f"{path}.{key}" if path else key) for key, child in value.items()}
        if isinstance(value, list):
            return [sample(child, f"{path}[{index}]") for index, child in enumerate(value)]
        return value

    return {
        "profile": sample(profile, ""),
        "series": summaries,
        "max_points_per_series": max_points,
        "notice": (
            "Series may be uniformly sampled. Minima and maxima use all original samples. "
            "Export the logbook for full-resolution analysis."
        ),
    }
