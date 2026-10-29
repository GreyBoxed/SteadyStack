"""Recovery codes: the way back in when the authenticator is gone.

Ten single-use codes. Each replaces one TOTP code and then dies, and they are
stored hashed by django-otp's static device, so nobody -- including whoever
runs the server -- can read them back. Shown exactly once, at the moment they
are made.

Losing both the authenticator and these means a shell on the box. That is the
cost of an account that controls a brokerage and has no reset link, and the
screen says so rather than letting somebody discover it later.
"""

import logging
import secrets

from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken

from audit.events import record
from audit.logging import fields

logger = logging.getLogger("steadystack.accounts")

DEVICE_NAME = "recovery"
HOW_MANY = 10

#: No vowels and no look-alikes: these get written down and read back by hand.
ALPHABET = "23456789bcdfghjkmnpqrstvwxz"


def _code() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{body[:4]}-{body[4:]}"


@transaction.atomic
def issue(user, *, actor: str) -> list[str]:
    """Replace any existing codes with ten fresh ones, and return them once.

    Replacing rather than adding: a set somebody half-remembers using is not a
    set they can rely on, and two live sets is one more than anybody can keep
    track of.
    """
    device, _ = StaticDevice.objects.get_or_create(user=user, name=DEVICE_NAME)
    replaced = device.token_set.count()
    device.token_set.all().delete()
    device.confirmed = True
    device.save(update_fields=["confirmed"])

    codes = [_code() for _ in range(HOW_MANY)]
    for code in codes:
        StaticToken.objects.create(device=device, token=code)

    record(
        "totp.recovery_issued",
        actor=actor,
        entity_type="otp_static.StaticDevice",
        entity_id=str(device.pk),
        after={"issued": HOW_MANY, "replaced": replaced},
    )
    logger.info(
        "recovery codes issued",
        extra=fields(user=user.get_username(), issued=HOW_MANY, replaced=replaced),
    )
    return codes


def remaining(user) -> int:
    """How many are still unused. The codes themselves are never readable."""
    device = StaticDevice.objects.filter(user=user, name=DEVICE_NAME).first()
    return device.token_set.count() if device else 0
