"""What address this system calls out from.

Zerodha restricts order placement to a registered address (M12a), so before
the engine places a live order it checks that the address it is calling from
is the one that was registered. That check needs an answer to a question the
process cannot answer locally: a laptop behind NAT and an EC2 instance behind
an elastic IP both see a private address on their own interfaces.

On EC2 the instance's metadata service knows: it reports the public address
-- the elastic IP -- without a request leaving AWS, so it is asked first
(IMDSv2, the token dance the box requires). Anywhere else that address
answers nothing, so it is given a fraction of a second and the question goes
to an external echo service instead. Three properties matter:

  * it fails closed. An unknown address is not a matching address, and live
    placement is refused rather than attempted (D12);
  * it is cached, because the answer changes about as often as the elastic IP
    does, and the check runs before every placement;
  * it never blocks for long. A gate that hangs is a missed tick.

The answer is advisory -- Zerodha's own rejection is authoritative -- but our
refusal comes first, which is the point: the order is never sent.
"""

import logging
import socket
import time
import urllib.error
import urllib.request

from django.conf import settings

from audit.logging import fields

logger = logging.getLogger("steadystack.broker")

#: Plain-text echo services returning nothing but an address. Tried in order.
DEFAULT_ECHO_URLS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)

DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_CACHE_SECONDS = 300.0

#: EC2's instance metadata service, IMDSv2: a PUT for a session token, then a
#: GET carrying it. The address is link-local, so it never leaves the host.
IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"  # noqa: S105 -- a URL
IMDS_PUBLIC_IPV4_URL = "http://169.254.169.254/latest/meta-data/public-ipv4"
IMDS_TOKEN_TTL_SECONDS = "60"  # noqa: S105 -- a lifetime
#: Short, because off EC2 nothing answers and a laptop waits this long once a
#: cache period. On the box the service answers in a millisecond or two.
DEFAULT_IMDS_TIMEOUT_SECONDS = 0.3

_cache: tuple[str, float] | None = None


def _echo_urls() -> tuple[str, ...]:
    return tuple(getattr(settings, "OUTBOUND_IP_ECHO_URLS", DEFAULT_ECHO_URLS))


def _timeout() -> float:
    return float(getattr(settings, "OUTBOUND_IP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))


def _cache_seconds() -> float:
    return float(getattr(settings, "OUTBOUND_IP_CACHE_SECONDS", DEFAULT_CACHE_SECONDS))


def _imds_timeout() -> float:
    return float(
        getattr(settings, "OUTBOUND_IP_IMDS_TIMEOUT_SECONDS", DEFAULT_IMDS_TIMEOUT_SECONDS)
    )


#: No proxies for the metadata service: a proxy cannot reach a link-local
#: address on this host, and would only turn a fast failure into a slow one.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(request, *, timeout: float, direct: bool = False):
    """The one place a request goes out, so tests can stand in for it."""
    if direct:
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


def _read(response) -> str:
    with response:
        return response.read().decode("utf-8", errors="replace").strip()


def _from_instance_metadata() -> str | None:
    """The public address EC2 has given this instance, or None off EC2.

    Failure is the normal case on a laptop and says nothing worth a warning,
    so it is logged at debug. An instance with no public address answers 404
    and falls through the same way.
    """
    timeout = _imds_timeout()
    try:
        token = _read(
            _urlopen(
                urllib.request.Request(
                    IMDS_TOKEN_URL,
                    method="PUT",
                    headers={"X-aws-ec2-metadata-token-ttl-seconds": IMDS_TOKEN_TTL_SECONDS},
                ),
                timeout=timeout,
                direct=True,
            )
        )
        body = _read(
            _urlopen(
                urllib.request.Request(
                    IMDS_PUBLIC_IPV4_URL, headers={"X-aws-ec2-metadata-token": token}
                ),
                timeout=timeout,
                direct=True,
            )
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("no instance metadata", extra=fields(error=str(exc)))
        return None

    if _looks_like_an_address(body):
        return body
    logger.warning("instance metadata returned junk for the public address")
    return None


def _looks_like_an_address(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
        except (OSError, ValueError):
            continue
        return True
    return False


def forget() -> None:
    """Drop the cached answer. Called after the registered address changes."""
    global _cache
    _cache = None


def outbound_ip(*, refresh: bool = False) -> str | None:
    """The public address this system appears to call from, or None.

    None means "could not be determined", which callers must treat as "does
    not match" -- never as "matches".
    """
    global _cache

    if _cache is not None and not refresh:
        value, fetched_at = _cache
        if time.monotonic() - fetched_at < _cache_seconds():
            return value

    from_ec2 = _from_instance_metadata()
    if from_ec2 is not None:
        _cache = (from_ec2, time.monotonic())
        return from_ec2

    for url in _echo_urls():
        try:
            body = _read(_urlopen(url, timeout=_timeout()))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("outbound address lookup failed", extra=fields(url=url, error=str(exc)))
            continue

        if _looks_like_an_address(body):
            _cache = (body, time.monotonic())
            return body

        logger.warning("outbound address lookup returned junk", extra=fields(url=url))

    return None
