"""Strip credentials from broker payloads before they are persisted.

Redaction is by key, deliberately. Guessing at values would be both unreliable
and destructive -- a price, an order id and a token are all just strings.

The key list is a deny-list, which means a credential under a name nobody
anticipated would pass through. That is the accepted trade: the alternative,
allowing only known-safe keys, silently discards the diagnostic detail these
records exist to preserve. New credential-bearing fields go in SENSITIVE_KEYS,
and any key that names a token is treated as one unless it is a known
identifier -- the one widening of the deny-list, because Kite's credentials
are almost all called something-token.

An error message is free text, so it cannot be redacted by key. `scrub`
removes the values the caller knows are secret -- the key, the secret and the
token the call was made with -- which is exact where guessing is not.
"""

from typing import Any

PLACEHOLDER = "[REDACTED]"

#: Compared after lowercasing and removing separators, so "AccessToken",
#: "access-token" and "ACCESS_TOKEN" all match one entry.
SENSITIVE_KEYS = frozenset(
    {
        "accesstoken",
        "publictoken",
        "refreshtoken",
        "requesttoken",
        "apikey",
        "apisecret",
        "secret",
        "password",
        "passwd",
        "authorization",
        "checksum",
        "totpsecret",
        "totpseed",
        "privatekey",
        "sessionid",
        "enctoken",
    }
)

#: Keys that end in "token" and name a scrip rather than a person. Every
#: quote and instrument carries them, and they are the diagnostic detail.
IDENTIFIER_TOKENS = frozenset({"instrumenttoken", "exchangetoken"})


def _normalise(key: str) -> str:
    return key.replace("_", "").replace("-", "").replace(" ", "").lower()


def _is_sensitive(key: str) -> bool:
    name = _normalise(key)
    if name in SENSITIVE_KEYS:
        return True
    return "token" in name and name not in IDENTIFIER_TOKENS


def redact(payload: Any) -> Any:
    """Return a copy of *payload* with credential-bearing values replaced.

    Handles dicts, lists and tuples recursively; anything else is returned as
    it came in.
    """
    if isinstance(payload, dict):
        return {
            key: PLACEHOLDER if _is_sensitive(str(key)) else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact(item) for item in payload)
    return payload


def scrub(text: str, *secrets: str | None) -> str:
    """Return *text* with every non-empty value in *secrets* replaced.

    Longest first, so a secret that contains another is removed whole.
    """
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, PLACEHOLDER)
    return text
