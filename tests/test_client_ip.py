"""Unit tests for `core.utils.client_ip`, which every per-IP rate limit is keyed on.

Two failure modes to stay away from, pulling in opposite directions:

- Trust nothing, and a deployment behind a reverse proxy sees the proxy's address for
  every caller. All the per-IP buckets become one global bucket, and one bot exhausting
  the magic-link limit locks sign-in out instance-wide.
- Trust `X-Forwarded-For` unconditionally, and any caller can forge a fresh identity per
  request and skip the limits entirely - worse than the bug it fixes.
"""

from unittest.mock import Mock, patch

import pytest

from src.app.core.utils import client_ip as client_ip_module
from src.app.core.utils.client_ip import client_ip, client_ip_bucket


def _request(peer: str | None, forwarded: str | None = None) -> Mock:
    request = Mock()
    request.client = Mock(host=peer) if peer is not None else None
    request.headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return request


@pytest.fixture(autouse=True)
def _clear_trusted_cache():
    """`_trusted_networks` is `lru_cache`d, so it has to be reset between settings."""
    client_ip_module._trusted_networks.cache_clear()
    yield
    client_ip_module._trusted_networks.cache_clear()


def _with_trusted(value):
    return patch.object(client_ip_module.settings, "TRUSTED_PROXY_IPS", value)


class TestWithoutTrustedProxies:
    """The default. Behaviour must be exactly the old socket-peer lookup."""

    def test_uses_the_socket_peer(self):
        with _with_trusted(None):
            assert client_ip(_request("203.0.113.7")) == "203.0.113.7"

    def test_ignores_a_forged_forwarded_header(self):
        with _with_trusted(None):
            assert client_ip(_request("203.0.113.7", "1.2.3.4")) == "203.0.113.7"

    def test_missing_peer_falls_back_to_a_shared_bucket(self):
        with _with_trusted(None):
            assert client_ip(_request(None)) == "unknown"


class TestBehindATrustedProxy:
    def test_takes_the_forwarded_address(self):
        with _with_trusted("10.0.0.5"):
            assert client_ip(_request("10.0.0.5", "203.0.113.7")) == "203.0.113.7"

    def test_accepts_a_cidr_block(self):
        """Proxies in Docker/Kubernetes get an address from a range, not a fixed one."""
        with _with_trusted("172.16.0.0/12"):
            assert client_ip(_request("172.18.0.4", "203.0.113.7")) == "203.0.113.7"

    def test_takes_the_rightmost_untrusted_hop(self):
        """A client controls the left of the chain - it can prepend anything it likes.
        Only the entries appended by trusted infrastructure, at the right, mean anything.
        """
        with _with_trusted("10.0.0.5, 10.0.0.6"):
            forwarded = "1.1.1.1, 203.0.113.7, 10.0.0.6"
            assert client_ip(_request("10.0.0.5", forwarded)) == "203.0.113.7"

    def test_a_spoofed_prefix_cannot_win(self):
        with _with_trusted("10.0.0.5"):
            forwarded = "9.9.9.9, 203.0.113.7"
            assert client_ip(_request("10.0.0.5", forwarded)) == "203.0.113.7"

    def test_untrusted_peer_is_used_even_when_a_header_is_present(self):
        """Someone reaching the app directly cannot promote themselves by sending the
        header - the peer has to be a declared proxy first.
        """
        with _with_trusted("10.0.0.5"):
            assert client_ip(_request("198.51.100.9", "203.0.113.7")) == "198.51.100.9"

    def test_trusted_proxy_with_no_header_falls_back_to_the_peer(self):
        with _with_trusted("10.0.0.5"):
            assert client_ip(_request("10.0.0.5")) == "10.0.0.5"

    def test_an_all_trusted_chain_falls_back_to_the_peer(self):
        with _with_trusted("10.0.0.0/8"):
            assert client_ip(_request("10.0.0.5", "10.1.1.1, 10.2.2.2")) == "10.0.0.5"

    def test_garbage_in_the_header_is_not_treated_as_trusted(self):
        with _with_trusted("10.0.0.5"):
            assert client_ip(_request("10.0.0.5", "not-an-ip")) == "not-an-ip"

    def test_whitespace_and_empty_entries_are_tolerated(self):
        with _with_trusted("10.0.0.5"):
            assert client_ip(_request("10.0.0.5", " 203.0.113.7 ,, ")) == "203.0.113.7"


class TestTheSettingParsesFromTheEnvironment:
    """Every test above patches `settings.TRUSTED_PROXY_IPS` directly, which is exactly
    how the first version of this shipped broken: the field was declared `list[str] | None`,
    and for a complex type pydantic-settings parses the environment variable itself and
    expects JSON. `TRUSTED_PROXY_IPS=172.16.0.0/12` then killed the app at startup with a
    validation error, while these unit tests stayed green because they never went through
    `Settings`. So: assert on the real thing.
    """

    def test_a_plain_cidr_string_is_accepted(self):
        from src.app.core.config import Settings

        settings = Settings(SECRET_KEY="test-only", TRUSTED_PROXY_IPS="172.16.0.0/12")

        assert settings.TRUSTED_PROXY_IPS == "172.16.0.0/12"

    def test_a_comma_separated_list_is_accepted(self):
        from src.app.core.config import Settings

        settings = Settings(SECRET_KEY="test-only", TRUSTED_PROXY_IPS="10.0.0.5, 172.16.0.0/12")

        with patch.object(client_ip_module.settings, "TRUSTED_PROXY_IPS", settings.TRUSTED_PROXY_IPS):
            client_ip_module._trusted_networks.cache_clear()
            assert client_ip(_request("172.18.0.4", "203.0.113.7")) == "203.0.113.7"
            assert client_ip(_request("10.0.0.5", "203.0.113.7")) == "203.0.113.7"


class TestRateLimitKeysActuallyDiffer:
    def test_two_callers_behind_one_proxy_get_different_keys(self):
        """The whole point: without this they share a bucket, and one of them can lock
        the other out of signing in.
        """
        with _with_trusted("10.0.0.5"):
            first = client_ip(_request("10.0.0.5", "203.0.113.7"))
            second = client_ip(_request("10.0.0.5", "198.51.100.4"))

        assert first != second


class TestClientIpBucket:
    """What a per-IP rate limit is keyed on. An IPv6 host is handed a whole /64, so keying
    on its full address would let one machine present a fresh bucket per request."""

    def test_an_ipv4_address_is_its_own_bucket(self):
        with _with_trusted(None):
            assert client_ip_bucket(_request("203.0.113.7")) == "203.0.113.7"

    def test_ipv6_addresses_in_one_64_share_a_bucket(self):
        with _with_trusted(None):
            one = client_ip_bucket(_request("2001:db8:1:2::1"))
            other = client_ip_bucket(_request("2001:db8:1:2:ffff:ffff:ffff:ffff"))

        assert one == other == "2001:db8:1:2::/64"

    def test_neighbouring_64s_do_not(self):
        with _with_trusted(None):
            assert client_ip_bucket(_request("2001:db8:1:2::1")) != client_ip_bucket(_request("2001:db8:1:3::1"))

    def test_an_ipv4_mapped_address_is_keyed_as_ipv4(self):
        with _with_trusted(None):
            assert client_ip_bucket(_request("::ffff:203.0.113.7")) == "203.0.113.7"

    def test_an_unknown_peer_stays_the_shared_bucket(self):
        with _with_trusted(None):
            assert client_ip_bucket(_request(None)) == "unknown"
