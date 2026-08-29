"""What a request tells us about *where* it came from, captured once and passed down.

Two attributes - the caller's IP and the raw `User-Agent` header - wanted by two features
at the same sites: a session row records the device that signed in, and an audit event
records the device an auth event happened from. Capturing them separately at each site
would be two spellings of the same thing at every token mint.

**Both values are attacker-supplied, and the bounds here are what makes that safe.** The
User-Agent is a request header with no length limit of its own. `client_ip` is less
obviously so: it returns the socket peer in the ordinary case, but behind a configured
`TRUSTED_PROXY_IPS` it returns the right-most `X-Forwarded-For` element that is not itself
a trusted proxy - and that element is a caller-written string which nothing validates as
an address (see `client_ip`, where `_is_trusted` answers `False` for anything unparseable
and the loop returns it as-is). So an over-length value must be truncated *here*, before it
reaches a column, rather than turning a sign-in into a 500 on a `StringDataRightTruncation`
- which is exactly what the audit design's "failures propagate, nothing is swallowed" rule
would otherwise cost on the one path that must never fail this way.

The two limits are also the two columns' widths (`models/user_session.py`,
`models/auth_audit_event.py` both import them), so the bound and the column cannot drift
apart into a truncation that still overflows.
"""

from dataclasses import dataclass

from fastapi import Request

from .client_ip import client_ip

# The longest textual IPv6 address is 45 characters (an IPv4-mapped form,
# `0000:...:255.255.255.255`), and `client_ip` can also answer the literal `"unknown"`.
# Nothing legitimate is longer; anything that is, is a forged header.
MAX_IP_LENGTH = 45

# Real User-Agent strings sit well under 200 characters even for the baroque ones. 400
# leaves room for something unusual while keeping a forged header from parking kilobytes
# on every session row an authenticated caller can mint.
MAX_USER_AGENT_LENGTH = 400


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Where one request came from, already bounded to what the columns take."""

    ip: str
    user_agent: str

    @classmethod
    def from_request(cls, request: Request) -> RequestContext:
        """Read and bound both values.

        The User-Agent is stored raw rather than parsed into "Chrome on macOS": the web
        client already ships that parser (`passkeyNameForUserAgent`), whose own header
        records the decision that only the client names the device and the server "sees a
        User-Agent header it has no business parsing". A second, diverging implementation
        here would let a session row and a passkey row on one settings page disagree about
        what to call the same browser.

        An absent header is `""` rather than a placeholder, because "this client sent no
        User-Agent" is a fact worth keeping distinct from any label we could invent for it.
        """
        return cls(
            ip=client_ip(request)[:MAX_IP_LENGTH],
            user_agent=request.headers.get("user-agent", "")[:MAX_USER_AGENT_LENGTH],
        )
