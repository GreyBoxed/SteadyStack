"""Replacing the authenticator, from the browser.

`enrol_totp` refuses to do this on a web page, reasoning that a page which can
add an authenticator turns one factor into two. That reasoning is right about
a page anybody with a session can use, and it is what this guards against:

  * starting a reset needs a code the operator holds *now* -- a current
    authenticator code, or a recovery code, which is exactly the case recovery
    codes exist for. A session alone cannot begin one.
  * the new device is provisioned unconfirmed and proves itself before the old
    one is revoked, so a reset abandoned halfway locks nobody out.
  * finishing it reissues the recovery codes, because an authenticator
    replaced under suspicion makes the codes issued beside it suspect too.

Console enrolment stays, and is still the only way in when every factor is
gone.
"""

import logging

from django.db import transaction
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import recovery
from audit.events import record
from audit.logging import fields

logger = logging.getLogger("steadystack.accounts")

#: Where the half-finished reset lives between the two steps.
SESSION_KEY = "totp_reset_device"

PENDING_NAME = "pending"


class ResetRefused(Exception):
    """A reset that was not allowed to start or finish."""


def holds_a_factor(user, token: str) -> bool:
    """Whether *token* proves possession of something the operator has.

    The authenticator, or a recovery code. A recovery code is accepted here
    and refused for rotating the codes themselves: losing the phone is the
    case they exist for, and minting more of themselves is not.
    """
    if not token:
        return False

    for device in TOTPDevice.objects.filter(user=user, confirmed=True):
        if device.verify_token(token):
            return True

    fallback = StaticDevice.objects.filter(
        user=user, name=recovery.DEVICE_NAME, confirmed=True
    ).first()
    return bool(fallback and fallback.verify_token(token))


def begin(user, *, token: str, actor: str) -> TOTPDevice:
    """Provision an unconfirmed device. Nothing is revoked yet.

    The check and its refusal sit outside the transaction deliberately: a
    refusal recorded inside the transaction that the refusal aborts is a
    refusal nobody can see afterwards, and somebody trying to replace the
    authenticator on a brokerage account is exactly the thing worth seeing.
    """
    if not holds_a_factor(user, token):
        record(
            "totp.reset_refused",
            actor=actor,
            entity_type="accounts.User",
            entity_id=str(user.pk),
            after={"reason": "no current authenticator or recovery code"},
        )
        raise ResetRefused(
            "That code did not verify. Replacing the authenticator needs a current "
            "code from it, or one of your recovery codes."
        )

    return _provision(user, actor=actor)


@transaction.atomic
def _provision(user, *, actor: str) -> TOTPDevice:
    TOTPDevice.objects.filter(user=user, name=PENDING_NAME, confirmed=False).delete()
    device = TOTPDevice.objects.create(user=user, name=PENDING_NAME, confirmed=False)

    record(
        "totp.reset_started",
        actor=actor,
        entity_type="otp_totp.TOTPDevice",
        entity_id=str(device.pk),
        after={"confirmed": False},
    )
    logger.info("totp reset started", extra=fields(user=user.get_username()))
    return device


def complete(user, *, device_id, token: str, actor: str) -> list[str]:
    """Confirm the new device, revoke the old, and reissue the codes.

    Verification sits outside the transaction for the same reason it does in
    `begin`: a refusal recorded inside the transaction that the refusal aborts
    is a refusal nobody can see afterwards.
    """
    try:
        pending = TOTPDevice.objects.get(pk=device_id, user=user, confirmed=False)
    except TOTPDevice.DoesNotExist as exc:
        raise ResetRefused("That reset is no longer in progress. Start it again.") from exc

    if not pending.verify_token(token):
        record(
            "totp.reset_refused",
            actor=actor,
            entity_type="otp_totp.TOTPDevice",
            entity_id=str(pending.pk),
            after={"reason": "the new device did not verify"},
        )
        raise ResetRefused(
            "That code did not match the new authenticator. Scan it again and "
            "enter the code it shows now."
        )

    return _swap(user, pending, actor=actor)


@transaction.atomic
def _swap(user, pending: TOTPDevice, *, actor: str) -> list[str]:
    """The irreversible half, once the replacement has proved itself.

    In this order deliberately: nothing is taken away until it has.
    """
    revoked = TOTPDevice.objects.filter(user=user, confirmed=True).exclude(pk=pending.pk)
    revoked_count = revoked.count()
    revoked.delete()

    pending.name = "default"
    pending.confirmed = True
    pending.save(update_fields=["name", "confirmed"])

    codes = recovery.issue(user, actor=actor)

    record(
        "totp.reset_completed",
        actor=actor,
        entity_type="otp_totp.TOTPDevice",
        entity_id=str(pending.pk),
        after={"revoked": revoked_count, "recovery_reissued": len(codes)},
    )
    logger.info(
        "totp reset completed",
        extra=fields(user=user.get_username(), revoked=revoked_count),
    )
    return codes


def qr_svg(device: TOTPDevice) -> str:
    """The pairing QR, as inline SVG.

    Rendered into the page rather than saved: the secret is not written to
    disk, not emailed, and not kept anywhere it could be read back.
    """
    import io

    import qrcode
    import qrcode.image.svg

    code = qrcode.QRCode(box_size=9, border=2)
    code.add_data(device.config_url)
    buffer = io.BytesIO()
    code.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    return buffer.getvalue().decode()
