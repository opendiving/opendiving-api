"""`species_service._worms` and the one segment percent-encoding cannot make safe.

Dots are unreserved, so `quote` leaves a segment of `.` or `..` as it is and httpx resolves it
away before sending: `?q=..` would ask for WoRMS's REST root rather than search for a name.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.core.config import settings
from src.app.services import species_service


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", [".", ".."])
async def test_a_dot_segment_never_leaves(segment: str) -> None:
    with patch.object(species_service, "_request", new_callable=AsyncMock) as request:
        assert await species_service._worms("AphiaRecordsByName", segment) is None

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dot_query_is_a_source_that_did_not_answer() -> None:
    """Not "WoRMS had nothing", which would be cached for a month under the diver's string."""
    with patch.object(species_service, "_request", new_callable=AsyncMock) as request:
        answer = await species_service._worms_by_name("..")

    request.assert_not_awaited()
    assert answer.ok is False
    assert answer.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("segment", "sent"), [("...", "..."), ("a..b", "a..b"), (". .", ".%20."), (127405, "127405")])
async def test_anything_else_with_dots_in_it_is_still_asked(segment: str | int, sent: str) -> None:
    with patch.object(species_service, "_request", new_callable=AsyncMock, return_value=[]) as request:
        await species_service._worms("AphiaRecordsByName", segment)

    assert request.await_args is not None
    (_, url, _) = request.await_args.args
    assert url == f"{settings.WORMS_API_URL.rstrip('/')}/AphiaRecordsByName/{sent}"
