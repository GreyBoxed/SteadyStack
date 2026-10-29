"""What the box must be, whatever its environment says.

Read from the production settings module directly, because the running
configuration in a test is the test configuration. The point of each one is
that it cannot be turned off by an environment variable: a production setting
a stray `DJANGO_DEBUG=true` can undo is one that will be undone at exactly the
wrong moment.

The module is re-imported with an environment of this file's choosing rather
than the one the suite happens to run under, so what is asserted is the
settings module's own behaviour and not the shell's.
"""

import importlib

import pytest

HOSTS = "steadystack.example,www.steadystack.example"


def load(monkeypatch, *, module="prod", absent=(), **environment):
    """Import the production settings under a stated environment.

    `base` is reloaded first: it is where the environment is actually read,
    and `prod` only re-exports what it computed. Reloading the module does not
    disturb `django.conf.settings`, which resolved long before this ran.

    The developer's .env is not read on the way: a variable this file leaves
    out must stay out, not be filled in from whatever the laptop has.
    """
    import environ

    monkeypatch.setattr(environ.Env, "read_env", classmethod(lambda cls, *a, **k: None))
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-only-secret-never-used-in-anger")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", HOSTS)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack",
    )
    monkeypatch.setenv("KITE_USER_ID", "AB1234")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    for name in absent:
        monkeypatch.delenv(name, raising=False)

    base = importlib.reload(importlib.import_module("config.settings.base"))
    if module == "base":
        return base
    return importlib.reload(importlib.import_module(f"config.settings.{module}"))


@pytest.fixture
def prod(monkeypatch):
    return load(monkeypatch)


def test_debug_is_off_whatever_the_environment_says(monkeypatch):
    """A traceback on this box contains the broker key."""
    assert load(monkeypatch, DJANGO_DEBUG="true").DEBUG is False


def test_https_is_enforced(prod):
    assert prod.SECURE_SSL_REDIRECT is True
    assert prod.SECURE_HSTS_SECONDS >= 31536000
    assert prod.SECURE_HSTS_INCLUDE_SUBDOMAINS is True


def test_the_cookies_never_leave_over_plain_http(prod):
    assert prod.SESSION_COOKIE_SECURE is True
    assert prod.CSRF_COOKIE_SECURE is True
    assert prod.SESSION_COOKIE_HTTPONLY is True


def test_sessions_are_short_lived(prod):
    """§3. A session that outlives the working day outlives the laptop it was
    opened on, and the cost of ending it is one more TOTP."""
    assert prod.SESSION_COOKIE_AGE <= 60 * 60 * 12
    assert prod.SESSION_EXPIRE_AT_BROWSER_CLOSE is True


def test_csrf_trusts_every_host_it_is_served_on(prod):
    """nginx terminates TLS, so Django sees a forwarded scheme and has to be
    told which origins that makes legitimate."""
    assert prod.ALLOWED_HOSTS == HOSTS.split(",")
    assert prod.CSRF_TRUSTED_ORIGINS == [f"https://{host}" for host in HOSTS.split(",")]


def test_the_pages_cannot_be_framed(prod):
    assert prod.X_FRAME_OPTIONS == "DENY"
    assert prod.SECURE_CONTENT_TYPE_NOSNIFF is True


def test_the_box_names_the_kite_account_it_trades(prod):
    assert prod.KITE_USER_ID == "AB1234"


def test_the_box_refuses_to_start_without_a_kite_account(monkeypatch):
    """Without it the callback would take a session for whichever Zerodha
    account signed in, and the next order would go there."""
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="KITE_USER_ID"):
        load(monkeypatch, KITE_USER_ID="")


# -- the owner's connection ------------------------------------------------


def test_the_owner_connection_exists_only_when_it_is_given(monkeypatch):
    """A runtime with no owner URL has no owner alias -- not a second copy of
    its own, which would make `migrate` look as though it had one."""
    base = load(monkeypatch, module="base", absent=("DATABASE_MIGRATION_URL",))

    assert set(base.DATABASES) == {"default"}


def test_the_owner_connection_is_used_when_it_is_given(monkeypatch):
    base = load(
        monkeypatch,
        module="base",
        DATABASE_MIGRATION_URL="postgres://steadystack_owner:owner@localhost:5432/steadystack",
    )

    assert base.DATABASES["migration"]["USER"] == "steadystack_owner"
