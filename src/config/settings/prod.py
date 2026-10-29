"""The box.

Everything here is a refusal rather than a preference. A production setting
that can be turned off by an environment variable is a production setting that
will be off at exactly the wrong moment.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

# Which Zerodha account this box trades. Without it the login callback would
# accept a session for whichever account signed in, and the next order would
# be placed there.
if not KITE_USER_ID:  # noqa: F405
    raise ImproperlyConfigured("KITE_USER_ID must name the Zerodha account this box trades.")

# Not read from the environment. `DJANGO_DEBUG=true` reaching this box would
# serve tracebacks containing the broker key to anybody who found a 500.
DEBUG = False

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# nginx terminates TLS and proxies to gunicorn, so Django sees the forwarded
# scheme above and must be told which origins that makes legitimate.
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS if host]  # noqa: F405

# Short-lived, as §3 asks. A session that outlives the working day is one that
# outlives the laptop it was opened on; the cost of it is one more TOTP.
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# The postback is the one endpoint the operator's browser never calls, and
# Kite's servers do not carry a referer. Django checks it only over HTTPS, so
# it is named rather than exempted.
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
