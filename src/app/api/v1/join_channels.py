"""Whether a join link is live, one slug at a time.

`/join?via=<slug>` is a link the operator posts somewhere public; following it admits the
visitor without an invitation. The web page asks here before it paints the sign-in form, so
a dead link shows the ordinary landing page instead.

**One slug per request, never the list.** A visitor asking about `scubaboard` learns about
`scubaboard` and not that `instagram` exists, which is why `GET /config` carries only
whether any channel exists. The route reads settings and no table, so it takes no rate
limiter, and a removed slug may resolve from a browser's cache for the minute
`ClientCacheMiddleware` allows - the gate, not this route, is what refuses it then.
"""

from fastapi import APIRouter

from ...core.config import settings
from ...core.exceptions.http_exceptions import NotFoundException
from ...schemas.join_channel import JoinChannelRead

router = APIRouter(tags=["config"])


@router.get("/join-channel/{slug}", response_model=JoinChannelRead)
async def read_join_channel(slug: str) -> JoinChannelRead:
    """The label a live join link is shown by, or 404.

    A bare `str` path parameter rather than one constrained to the slug format: a segment
    no operator could have configured is simply not a key, and answers the same 404 as a
    well-formed one that is not configured, so the route has one answer for "no such link".
    """
    label = settings.join_channels.get(slug)
    if label is None:
        raise NotFoundException("This join link is not active.")
    return JoinChannelRead(slug=slug, label=label)
