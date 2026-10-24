"""Open-Meteo behind `httpx.MockTransport`, answering from recorded fixtures.

Patches the client *construction* in `core.utils.outbound`, as the geocoder's tests do, so the
URL, parameters and headers the client really builds are under test and nothing leaves the
machine. Every request is recorded in `requests`.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "conditions"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return body


def dahab_body(request: httpx.Request) -> dict[str, Any]:
    """The Blue Hole's recorded answer to `request`, moved to the days it asked about.

    Its 2026-09-15 weather in UTC days, its January weather in the zone the provider resolves
    for a wall clock, its marine series around 2026-09-15 - each with every sample shifted by
    whole days so the series starts on the requested `start_date`, so a dive on any date reads
    the recorded hours as its own.
    """
    if "marine" in request.url.host:
        body, recorded = fixture("dahab-2026-09-15-marine.json"), date(2026, 9, 14)
    elif request.url.params.get("timezone") == "auto":
        body, recorded = fixture("dahab-2026-01-15-weather-auto.json"), date(2026, 1, 14)
    else:
        body, recorded = fixture("dahab-2026-09-15-weather.json"), date(2026, 9, 15)
    shift = (date.fromisoformat(request.url.params["start_date"]) - recorded).days * 86400
    body["hourly"]["time"] = [at + shift for at in body["hourly"]["time"]]
    return body


def dahab_answers(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=dahab_body(request))


class FakeOpenMeteo:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response] = dahab_answers) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler
        self._patcher: Any = None

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def __enter__(self) -> FakeOpenMeteo:
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.core.utils.outbound.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()

    def asked(self, kind: str) -> list[httpx.Request]:
        host = "marine" if kind == "sea" else "archive"
        return [request for request in self.requests if host in request.url.host]
