"""What `GET /config` tells an anonymous browser about this instance.

Named for the thing rather than for the setting: `ConfigContext` is already the web app's
word for "what this instance is configured to do", and the endpoint was expected to grow a
second field before it grew a second route - which is what `project_operated` is.
"""

from pydantic import BaseModel

from ..core.config import RegistrationMode


class InstanceConfigRead(BaseModel):
    """None of these fields discloses anything a visitor could not already see.

    In `invite` mode the landing page shows a request-an-invite form and in `open` mode a
    sign-in form, so the mode is legible from the page itself; and that form carries either
    the project's own waitlist wording or the generic wording true of any instance, which is
    how `project_operated` shows. Publishing them here only saves the client from guessing.
    What it buys is that the *API* stays the single source of each - the alternative, a copy
    in the web container's own environment, would be a second place for it to be wrong,
    would make flipping it a web restart as well as an API one (the web memoises its runtime
    config for the life of the process), and would be a compose change that an existing
    install's `docker compose pull` does not deliver.

    `project_operated` has a second reason to be API truth, and it is no longer a prospective
    one: this API reads the same field for its own invitation email, whose opening sentence
    invites you to OpenDiving rather than to somebody's log book where the project runs the
    instance (`services.email_service.send_invitation_email`). A web-side variable could
    never have reached that template.
    """

    registration_mode: RegistrationMode
    project_operated: bool
    # Whether any join link exists here - a yes or no, never the list, which is resolved one
    # slug at a time (`GET /join-channel/{slug}`) so a visitor holding one link learns
    # nothing about the others. The privacy page shows its join-link paragraph on it.
    join_links: bool
    # Whether this instance names a map renderer, so the web can say what its cards show and
    # what its privacy page owes without probing a service only the API can reach.
    map_pictures: bool
