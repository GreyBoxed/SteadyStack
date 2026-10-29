"""The operator's own account: what is left of the way back in.

Recovery codes are shown once and stored hashed, so this page can say how many
remain and issue a fresh set — it cannot show the current ones, and neither
can anybody else.
"""

import logging

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from accounts import recovery, totp_reset
from audit.events import record

logger = logging.getLogger("steadystack.accounts")


def _authenticator_accepts(user, token: str) -> bool:
    """Whether a current code from the operator's authenticator verifies.

    Deliberately the authenticator and not a recovery code: rotating the
    fallback must require the primary factor, or one stolen code mints ten
    more. Somebody who has lost the phone enrols a new one from a shell, and
    that issues a fresh set anyway.
    """
    from django_otp.plugins.otp_totp.models import TOTPDevice

    if not token:
        return False
    return any(
        device.verify_token(token)
        for device in TOTPDevice.objects.filter(user=user, confirmed=True)
    )


# It shows recovery codes once; a cached copy would show them again.
@never_cache
@require_http_methods(["GET", "POST"])
def security(request: HttpRequest) -> HttpResponse:
    issued = request.session.pop("totp_reset_codes", None)

    if request.method == "POST":
        # A live session is not enough to rotate the way back in. One that has
        # been taken over could otherwise kill the operator's codes and keep
        # ten of its own, which outlives the session it came from.
        if not _authenticator_accepts(request.user, request.POST.get("otp_token", "").strip()):
            record(
                "totp.recovery_refused",
                actor=request.user.get_username(),
                entity_type="accounts.User",
                entity_id=str(request.user.pk),
                after={"reason": "authenticator code did not verify"},
            )
            messages.error(
                request,
                "That code did not verify. Rotating recovery codes needs a current "
                "code from your authenticator, not just a signed-in session.",
            )
        else:
            issued = recovery.issue(request.user, actor=request.user.get_username())
            messages.success(
                request,
                "A fresh set. The previous codes are dead — save these somewhere "
                "that is not this machine.",
            )

    from django_otp.plugins.otp_totp.models import TOTPDevice

    pending = _pending_device(request)

    return render(
        request,
        "accounts/security.html",
        {
            "issued": issued,
            "remaining": recovery.remaining(request.user),
            "how_many": recovery.HOW_MANY,
            "devices": TOTPDevice.objects.filter(user=request.user, confirmed=True),
            "pending": pending,
            "pending_qr": totp_reset.qr_svg(pending) if pending else None,
        },
    )


def _pending_device(request):
    """The half-finished reset, if one is in progress in this session."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device_id = request.session.get(totp_reset.SESSION_KEY)
    if not device_id:
        return None
    return TOTPDevice.objects.filter(pk=device_id, user=request.user, confirmed=False).first()


@require_POST
def reset_totp(request: HttpRequest) -> HttpResponse:
    """Start replacing the authenticator, or finish it.

    Starting needs a factor the operator holds now; finishing needs a code
    from the new device. Nothing is revoked until the replacement works.
    """
    from django.shortcuts import redirect

    step = request.POST.get("step", "")
    actor = request.user.get_username()

    try:
        if step == "begin":
            device = totp_reset.begin(
                request.user, token=request.POST.get("otp_token", "").strip(), actor=actor
            )
            request.session[totp_reset.SESSION_KEY] = str(device.pk)
            messages.info(
                request,
                "Scan the code below, then enter what your authenticator shows. "
                "Your current one keeps working until you do.",
            )
        elif step == "confirm":
            codes = totp_reset.complete(
                request.user,
                device_id=request.session.get(totp_reset.SESSION_KEY),
                token=request.POST.get("confirm_token", "").strip(),
                actor=actor,
            )
            request.session.pop(totp_reset.SESSION_KEY, None)

            # The session was verified against the device just revoked, so
            # without this the operator is bounced to the login page before
            # ever seeing the codes they were handed. They proved possession
            # of the new device a moment ago; that is what a verified session
            # means.
            from django_otp import login as otp_login
            from django_otp.plugins.otp_totp.models import TOTPDevice

            replacement = TOTPDevice.objects.filter(user=request.user, confirmed=True).first()
            if replacement is not None:
                otp_login(request, replacement)

            request.session["totp_reset_codes"] = codes
            messages.success(
                request,
                "Replaced. The old authenticator no longer works, and your recovery "
                "codes have been reissued below.",
            )
        elif step == "cancel":
            _discard_pending(request)
            messages.info(request, "Left as it was. Your authenticator is unchanged.")
        else:
            messages.error(request, "That is not a step of a reset.")
    except totp_reset.ResetRefused as exc:
        messages.error(request, str(exc))

    return redirect("accounts:security")


def _discard_pending(request) -> None:
    from django_otp.plugins.otp_totp.models import TOTPDevice

    device_id = request.session.pop(totp_reset.SESSION_KEY, None)
    if device_id:
        TOTPDevice.objects.filter(pk=device_id, user=request.user, confirmed=False).delete()
