"""The login form.

django-otp's OTPAuthenticationForm refuses a token that does not name the
device it belongs to. That refusal is well-founded: trying a token against
every device in turn records a failed attempt on each one that does not match,
which throttles devices the operator never touched.

It also assumes a user with several devices, and produces a two-step login --
submit the password, receive a device list, submit again. This system has one
operator with one authenticator, so the choice is between a second page that
exists only to confirm the obvious, and preselecting the device when there is
exactly one to preselect.

Preselecting is safe for the reason the refusal exists: with a single device
there is no second device to throttle. Two or more, and the explicit choice is
required again, because then the original hazard is real.
"""

from django import forms
from django_otp import devices_for_user
from django_otp.forms import OTPAuthenticationForm
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import recovery


class OperatorAuthenticationForm(OTPAuthenticationForm):
    def clean(self):
        """Refuse every failure in the same words.

        django-otp checks the password first and then says what is wrong with
        the code -- missing, wrong, which device, too many tries -- and each
        of those messages is only ever shown to somebody whose password was
        right. That makes the password guessable without the code. So any
        refusal, whichever factor caused it, is Django's own invalid_login,
        and nothing the first factor unlocked is left on the form.
        """
        try:
            return super().clean()
        except forms.ValidationError:
            self.user_cache = None
            # Set by django-otp once the password verifies: the password
            # re-rendered into the page, and the device list filled in.
            self.fields["password"].widget.render_value = False
            if "otp_device" in self.fields:
                self.fields["otp_device"].widget.choices = []
            raise self.get_invalid_login_error() from None

    def _chosen_device(self, user):
        chosen = super()._chosen_device(user)
        if chosen is not None:
            return chosen

        # for_verify locks the row; clean_otp already holds a transaction.
        devices = list(devices_for_user(user, for_verify=True))
        if len(devices) == 1:
            return devices[0]

        # The recovery device is not a second authenticator, it is the way
        # back into the first one. Preselect the TOTP device when there is
        # exactly one, and let `_verify_token` reach the codes if the token is
        # not a TOTP code.
        totp = [device for device in devices if isinstance(device, TOTPDevice)]
        if len(totp) == 1:
            return totp[0]

        # Genuinely several authenticators: the original hazard is real again,
        # so the operator names the one they used.
        return None

    def _verify_token(self, user, token, device=None):
        """Try the authenticator, then the recovery codes.

        django-otp refuses to try every device because a wrong token would
        record a failure against each one, throttling devices nobody touched.
        That reasoning holds, which is why this tries exactly two and only
        two: the operator's authenticator, and the codes that exist to stand
        in for it. The account-level throttle in `accounts.throttle` is what
        actually locks the door, and it counts attempts, not devices.
        """
        try:
            return super()._verify_token(user, token, device)
        except forms.ValidationError:
            recovery_device = StaticDevice.objects.filter(
                user=user, name=recovery.DEVICE_NAME, confirmed=True
            ).first()
            if recovery_device is None or device is recovery_device:
                raise
            return super()._verify_token(user, token, recovery_device)
