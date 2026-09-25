"""Structural guard: every `/api/v1` route requires authentication unless allowlisted.

The security audit that prompted this fixed seven defects, every one a point fix, and
nothing stopped the next PR from reintroducing the class. This repo's own history is the
argument that it would: the cache-labelling defect came back in a second shape with a
different credential (see `test_client_cache_middleware.py`).

So this is the enumerate-and-allowlist shape the suite already uses in three places -
`test_every_update_schema_is_accounted_for`, `test_no_token_minting_route_is_a_safe_method`,
`test_every_credential_header_is_named`. A new route that forgets `get_current_user`
fails here rather than shipping, and the only way past is to write down *why* it is
anonymous where a reviewer will read it.

What this does **not** check is authorization: that a route which authenticates the
caller then serves only that caller's rows. That is the other guard, in
`test_ownership.py`.
"""

from typing import Any

from src.app.api.dependencies import get_current_superuser, get_current_user
from src.app.core.security import oauth2_scheme
from src.app.main import app
from tests.helpers.routes import RouteInfo, dependency_calls, iter_api_routes

# Any one of these on a route means a request without credentials never reaches the
# handler. `oauth2_scheme` counts on its own: it is built `auto_error=True`
# (`core/security.py`), so a missing `Authorization` header is a 401 before the handler
# runs. `POST /auth/logout` has it and nothing else, and calling that "no auth" would put
# an authenticated route into an allowlist of anonymous ones - exactly the wrong
# direction for a guard whose failure mode is fail-open.
AUTH_MARKERS: set[Any] = {get_current_user, get_current_superuser, oauth2_scheme}

# The reason is the payload here, not the path. It is what a reviewer reads when someone
# proposes the next entry, and "it needs to be anonymous" is not one of these reasons.
# They fall into the numbered groups below; the numbers are labels rather than a count, so
# adding a group is adding a group.
ANONYMOUS_BY_DESIGN: dict[tuple[str, str], str] = {
    # 1. The caller has no token yet, by definition - these *are* the flow that issues one.
    ("POST", "/api/v1/auth/email/request"): "Asks for a magic link; nobody is signed in at the start of sign-in.",
    ("GET", "/api/v1/auth/email/verify/check"): "Reads back the address a link stands for, before a session exists.",
    ("POST", "/api/v1/auth/email/verify"): "Redeems the magic link - the request that mints the first session.",
    (
        "POST",
        "/api/v1/auth/email/verify-code",
    ): "Redeems the code from the same email as that link, at the same point in the flow.",
    (
        "POST",
        "/api/v1/auth/google",
    ): "Redeems a Google authorization code; same position in the flow as email/verify.",
    ("POST", "/api/v1/auth/complete"): "Turns an onboarding token into an account - there is no account yet.",
    (
        "POST",
        "/api/v1/auth/refresh",
    ): "Trades a refresh token for an access token, which an expired session has instead.",
    ("POST", "/api/v1/auth/passkey/options"): (
        "Hands out a challenge to sign in with; naming an account first is exactly what "
        "discoverable credentials exist to avoid."
    ),
    ("POST", "/api/v1/auth/passkey/verify"): (
        "Redeems a passkey assertion - the signature is the credential, same position in the flow as email/verify."
    ),
    ("POST", "/api/v1/auth/restore"): (
        "Undoes a deletion, so it serves exactly the accounts `get_current_user` filters "
        "out - the restore token is the credential, and it names the account itself."
    ),
    # 2. The caller may be locked out, and that is the point.
    ("POST", "/api/v1/support"): "A diver who cannot sign in is precisely who needs to reach a human.",
    ("POST", "/api/v1/invite-requests"): (
        "Asks a closed instance for an invitation; the whole population it serves is people with no "
        "account. It answers the same 202 for every address and never queries `user`, which is the "
        "same structural guarantee `/auth/email/request` carries and for the same reason."
    ),
    # 3. The caller is a monitor or an orchestrator, holding no account at all.
    ("GET", "/api/v1/health"): "Liveness probe, read by whatever decides whether to restart the container.",
    ("GET", "/api/v1/health/ready"): "Readiness probe, same caller as /health.",
    # The interesting pair: authorized by the token in the link rather than by a session,
    # because the link may be opened on a different device than the one that asked for the
    # change - see the `verify_email_change` docstring. Both carry that token in the query
    # string, and so opt out of public caching by hand (`TestExplicitHeadersWin` in
    # test_client_cache_middleware.py).
    ("GET", "/api/v1/user/email-change/verify/check"): "The link's token is the credential, not a session.",
    ("POST", "/api/v1/user/email-change/verify"): "Same token-in-the-link authorization as its /check counterpart.",
    # 4. The caller is an `<img>` tag, which carries no credential and cannot be given one.
    ("GET", "/api/v1/species/{uuid}/photo"): (
        "Serves a public Wikimedia Commons image from a global, ownerless catalog to an "
        "`<img src>`, which cannot send a Bearer token - and the cookie this app sets is the "
        "refresh token, read on three auth paths only. Discloses nothing: a species uuid is "
        "not an existence oracle for anything private, and the bytes are freely licensed "
        "files anybody can fetch from Commons directly. Serving them from here is what stops "
        "a diver's browser telling Wikimedia which species they are looking at."
    ),
    # 5. The caller is a browser deciding what to render before anyone has signed in.
    ("GET", "/api/v1/config"): (
        "Tells the landing page whether registration is open or by invitation, and whether the project "
        "itself operates the instance, which it has to know before its first paint - and before any "
        "session exists. Discloses two bits the page discloses anyway: by which form it then shows, "
        "and by which copy that form carries."
    ),
}


def _is_authenticated(route: RouteInfo) -> bool:
    return any(call in AUTH_MARKERS for call in dependency_calls(route.dependant))


def test_every_api_route_requires_authentication() -> None:
    """The guard. A route with no auth marker has to be in `ANONYMOUS_BY_DESIGN`."""
    anonymous = {route.key for route in iter_api_routes(app) if not _is_authenticated(route)}

    unaccounted = sorted(anonymous - ANONYMOUS_BY_DESIGN.keys())

    assert not unaccounted, (
        "these routes serve anyone who can reach the port:\n"
        + "\n".join(f"  {method:6} {path}" for method, path in unaccounted)
        + "\n\nAdd `current_user: Annotated[dict, Depends(get_current_user)]` to the handler."
        + "\nIf it is genuinely meant to be anonymous, add it to `ANONYMOUS_BY_DESIGN` with"
        + "\nthe reason - a sentence a reviewer can disagree with, not a restatement of the path."
    )


def test_no_allowlist_entry_is_stale() -> None:
    """The drift guard pointing the other way, and the one that matters more over time.

    An entry whose route was renamed, removed, or has since grown a `get_current_user`
    guards nothing - and worse, it sits there pre-approving the path, so a later route
    registered at the same one is waved through without anyone deciding that. Same reason
    `test_every_declared_field_exists_on_the_schema` exists next to its own sweep.

    It also fails loudly if `iter_api_routes` ever stops finding routes at all, which is
    what would otherwise turn the guard above into a test that passes vacuously.
    """
    anonymous = {route.key for route in iter_api_routes(app) if not _is_authenticated(route)}

    stale = sorted(ANONYMOUS_BY_DESIGN.keys() - anonymous)

    assert not stale, (
        "these `ANONYMOUS_BY_DESIGN` entries no longer name an anonymous route:\n"
        + "\n".join(f"  {method:6} {path}" for method, path in stale)
        + "\n\nDelete them. Leaving one behind pre-allowlists the path for whatever is registered there next."
    )


def test_the_bearer_scheme_alone_counts() -> None:
    """`POST /auth/logout` depends on `oauth2_scheme` and on nothing else in `AUTH_MARKERS`.

    That makes it the canary for the walk itself. FastAPI moved security schemes out of
    `Dependant.security_requirements` and into ordinary sub-dependencies in 0.141; if a
    future version moves them somewhere `dependency_calls` doesn't look, this route is the
    first - and for a while the only - one to go quiet, and a single unexplained failure in
    the sweep above reads like a routing mistake rather than a broken walk.
    """
    logout = next(route for route in iter_api_routes(app) if route.key == ("POST", "/api/v1/auth/logout"))

    calls = dependency_calls(logout.dependant)

    assert oauth2_scheme in calls
    assert get_current_user not in calls
    assert _is_authenticated(logout)
