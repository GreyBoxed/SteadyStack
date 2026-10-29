import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-never-used-in-anger")
os.environ.setdefault(
    "DATABASE_URL", "postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack"
)

from .base import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast tests only

# The order lock is a file every order-touching process on the machine shares.
# A test holding the real one would block a trading day running alongside it,
# and a round of that day would make the test report "Busy". Each test run
# gets a lock of its own; a test that exercises the lock sets its own path.
ORDER_LOCK_PATH = os.path.join(tempfile.mkdtemp(prefix="steadystack-test-"), "orders.lock")

# Tests already connect as the schema owner, and the test database is created
# and migrated by the runner. Leaving a second alias here would send that
# migration at the development database instead of the test one.
DATABASES.pop("migration", None)  # noqa: F405
