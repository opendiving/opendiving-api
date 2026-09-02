"""What `GET /config` tells an anonymous browser about this instance.

Named for the thing rather than for the setting: `ConfigContext` is already the web app's
word for "what this instance is configured to do", and the endpoint is expected to grow a
second field before it grows a second route.
"""

from pydantic import BaseModel

from ..core.config import RegistrationMode


class InstanceConfigRead(BaseModel):
    """One field, and it discloses nothing a visitor could not already see.

    In `invite` mode the landing page shows a request-an-invite form and in `open` mode a
    sign-in form, so the mode is legible from the page itself; publishing it here only
    saves the client from guessing. What it buys is that the *API* stays the single source
    of the mode - the alternative, a copy in the web container's own environment, would be
    a second place for it to be wrong, would make flipping it a web restart as well as an
    API one (the web memoises its runtime config for the life of the process), and would
    be a compose change that an existing install's `docker compose pull` does not deliver.
    """

    registration_mode: RegistrationMode
