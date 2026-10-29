"""Settings shared by every environment.

Secrets are read from the environment. Locally that is a gitignored .env; in
production the same names are populated from AWS Secrets Manager (Phase 4), so
nothing downstream changes.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parents[3]

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    KITE_API_KEY=(str, ""),
    KITE_API_SECRET=(str, ""),
    KITE_TOKEN_KEY=(str, ""),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    # Before staticfiles on purpose: Django resolves a management command to
    # the first app that defines it, and `web` overrides runserver to print
    # the state of the installation on the way up.
    "web",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "accounts",
    "audit",
    "broker",
    "configuration",
    "engine",
    "model_portfolio",
    "orders",
    "portfolio",
    "records",
    "rebalance",
    "gains",
    "journal",
    "performance",
    "trading_calendar",
]

MIDDLEWARE = [
    # Outermost: a request rejected further down still produces traceable
    # log lines, and a rejected request is the one somebody comes looking for.
    "audit.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # Before authentication is enforced: a phone asking for / is sent to /m/
    # first, so the sign-in it is then asked for is the phone's.
    "web.device.DeviceRedirectMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Populates request.user.is_verified(). Enforces nothing on its own.
    "django_otp.middleware.OTPMiddleware",
    # Enforcement is ours: deny by default, explicit allowlist.
    "accounts.middleware.OTPEnforcementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "src" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.chrome.chrome",
                "web.chrome.asset_version",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Two connections to one database. The runtime role has UPDATE and DELETE
# revoked on the append-only tables, which is what makes the invariant real --
# and which also means it cannot create them. Migrations therefore run as the
# schema owner, over the "migration" alias (see audit's migrate command).
#
# The alias exists only when the owner's URL is given. A process that is not
# migrating has no business holding the owner's credentials, and an alias
# quietly defaulting to the runtime role would make `migrate` claim the owner's
# connection while running as the role that cannot create a table.
DATABASES = {"default": env.db("DATABASE_URL")}
if env("DATABASE_MIGRATION_URL", default=""):
    DATABASES["migration"] = env.db("DATABASE_MIGRATION_URL")

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# NSE runs on IST. EC2 defaults to UTC. Pin it here so trading_date is never
# ambiguous; a timezone assumption corrected later is a data migration.
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# One JSON object per line, everywhere. Pretty locally and structured in
# production would mean the format that matters is the one nobody tested.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "audit.logging.JSONFormatter"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "steadystack": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

STATIC_URL = "static/"
# The Tailwind build output is committed, so the box needs no Node at deploy
# time. Rebuild with `npm run build` after changing any template's classes.
STATICFILES_DIRS = [BASE_DIR / "src" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "/"
# Signing out lands on the sign-in page, not Django's own "logged out" page,
# which would sit outside the design and say nothing useful.
LOGOUT_REDIRECT_URL = "accounts:login"

# Kite API credentials. Reads work in every environment; whether an order may
# be *placed* is decided by broker.mode against database configuration and the
# registered static IP (M12a, M19b), never by an environment variable.
KITE_API_KEY = env("KITE_API_KEY")
KITE_API_SECRET = env("KITE_API_SECRET")

# The Zerodha client ID this installation trades for. The login callback
# refuses a session for any other account: a request token says which account
# signed in, not which one was meant to. Required on the box (prod.py); left
# empty on a laptop, the check is skipped with a warning.
KITE_USER_ID = env("KITE_USER_ID", default="").strip().upper()

# Fernet key for the access token at rest. Absent, no session can be stored --
# deliberately, rather than falling back to plaintext.
KITE_TOKEN_KEY = env("KITE_TOKEN_KEY")

# The one lock every order-touching process shares (orders.idempotency): the
# timers and the web app alike. On the box it lives in the units' shared
# StateDirectory, /var/lib/steadystack, which the service user can write and
# which -- unlike /tmp under PrivateTmp -- every unit sees the same.
ORDER_LOCK_PATH = env("ORDER_LOCK_PATH", default="/tmp/steadystack-orders.lock")  # noqa: S108
