"""What an anonymous browser may know about how this instance is configured.

Two fields - the registration mode, and whether the OpenDiving project itself operates
this instance - because the web app has to decide, before its landing page paints, whether
the hero shows a sign-in form or a request-an-invite form and, when the latter, whether that
form carries the project's waitlist wording or the generic wording true of any instance. It
has no other channel by which to learn either.

**An endpoint rather than a copy in the web container's own environment**, and the reason
is three-fold. The mode is API truth, and the app has a recorded precedent for what two
copies cost: the web mirrors Google's client id in its own environment and consequently
"has no way to learn that the API lacks a secret", which is why *this* app refuses to boot
in that state rather than letting the web find out. The web memoises its runtime config for
the life of the process, so an env var would make flipping the mode a web restart as well
as an API one. And the install bundle passes this container the whole `.env` while handing
the web a curated list, so a web-side setting is a compose change that `docker compose pull`
does not deliver to an install that already exists, while an endpoint ships with the image.
"""

from fastapi import APIRouter

from ...core.config import settings
from ...schemas.instance_config import InstanceConfigRead

router = APIRouter(tags=["config"])


@router.get("/config", response_model=InstanceConfigRead)
async def read_instance_config() -> InstanceConfigRead:
    """This instance's public configuration.

    Anonymous, and not a leak: the landing page discloses the mode anyway by which form it
    shows, and the operator by which copy that form carries, so this only saves a client
    from inferring them. Nothing here is per-caller, which is what makes it safe for
    `ClientCacheMiddleware` to mark publicly cacheable - and a minute of caching is right,
    since the values change only when the operator restarts the API.
    """
    return InstanceConfigRead(registration_mode=settings.REGISTRATION_MODE, project_operated=settings.PROJECT_OPERATED)
