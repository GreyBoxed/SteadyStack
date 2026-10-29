"""Enrol a TOTP device from the console.

First enrolment lives here because there is nowhere else it could: the
operator cannot sign in to a page before they have a factor to sign in with.
It is also the way back when every factor is gone.

*Replacing* an authenticator is reachable from the browser as well
(`accounts/security/`), under the guard this docstring used to argue for: a
page that can add an authenticator would turn one factor into two, so that
page refuses to start without a code the operator already holds, and revokes
nothing until the replacement has proved itself. The secret is rendered into
the page and never emailed or written to disk.
"""

import qrcode
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import recovery
from audit.events import record


class Command(BaseCommand):
    help = "Create a confirmed TOTP device for an operator and print its QR code."

    def add_arguments(self, parser) -> None:
        parser.add_argument("username")
        parser.add_argument(
            "--name",
            default="default",
            help="Device name, so a replacement phone is distinguishable from the old one.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Remove this operator's existing devices of the same name first.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        username = options["username"]
        name = options["name"]

        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=username)
        except user_model.DoesNotExist as exc:
            raise CommandError(f"No user named {username!r}.") from exc

        existing = TOTPDevice.objects.filter(user=user, name=name)
        if existing.exists():
            if not options["replace"]:
                raise CommandError(
                    f"{username} already has a device named {name!r}. "
                    "Pass --replace to revoke it and enrol a new one."
                )
            count = existing.count()
            existing.delete()
            record(
                "totp.device_revoked",
                actor=username,
                entity_type="otp_totp.TOTPDevice",
                entity_id=name,
                after={"revoked": count},
            )

        device = TOTPDevice.objects.create(user=user, name=name, confirmed=True)
        record(
            "totp.device_enrolled",
            actor=username,
            entity=device,
            after={"name": name},
        )

        self.stdout.write("")
        self.stdout.write(f"Scan this with an authenticator app for {username}:")
        self.stdout.write("")
        qr = qrcode.QRCode()
        qr.add_data(device.config_url)
        qr.print_ascii(out=self.stdout)
        self.stdout.write("")
        self.stdout.write("The secret is not printed. If the scan fails, run again with --replace.")
        self.stdout.write(
            self.style.WARNING("Verify a code before logging out of any existing session.")
        )

        # The authenticator alone leaves no way back. Ten single-use codes,
        # stored hashed, printed here and never again.
        codes = recovery.issue(user, actor=username)
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Recovery codes -- shown once, stored hashed:"))
        for code in codes:
            self.stdout.write(f"  {code}")
        self.stdout.write("")
        self.stdout.write(
            "Save these somewhere that is not this machine. Lose both the "
            "authenticator and these and the only way back in is a shell on the box."
        )
