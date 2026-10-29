"""The daily Kite session.

An access token lives for one day. That single fact shapes the operator's
whole relationship with this system: the one recurring act is a morning login,
and a day without one is a day that buys nothing.

The token is encrypted at rest. Not because the database is expected to leak,
but because this row is the one thing in the schema that is *usable* on its
own -- with it, anybody can place orders in a real brokerage account until it
expires. Everything else here is a record of what happened.
"""

import uuid

from django.db import models

from broker.crypto import decrypt, encrypt


class KiteSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)

    kite_user_id = models.CharField(max_length=32, blank=True)
    user_name = models.CharField(max_length=150, blank=True)

    api_key = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text=(
            "The app this token was issued for. A token belongs to a key: change "
            "the key and every token issued under the old one is worthless."
        ),
    )

    #: Ciphertext. The plaintext never exists outside `access_token`.
    encrypted_access_token = models.BinaryField()

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=255, blank=True)
    established_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at"]
        get_latest_by = "created_at"

    def __str__(self) -> str:
        # Deliberately says nothing about the token itself: this string ends
        # up in admin pages, logs and exception messages.
        who = self.kite_user_id or "unknown"
        return f"kite session for {who} until {self.expires_at:%Y-%m-%d %H:%M}"

    @property
    def access_token(self) -> str:
        """The plaintext token, decrypted on use and never stored anywhere."""
        return decrypt(bytes(self.encrypted_access_token))

    def set_access_token(self, value: str) -> None:
        self.encrypted_access_token = encrypt(value)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class KiteLoginState(models.Model):
    """One started login, waiting for Kite to hand its state back.

    The callback is outside the OTP gate, so a request token arriving there
    proves only that somebody signed in to Zerodha -- possibly as themselves,
    with the link then handed to the operator. The state is what ties a
    callback to a login this system started: minted by `start_login`, carried
    through Kite's `redirect_params`, good for ten minutes and for one use.

    Stored as a digest, so a read of this table yields nothing a caller could
    still present.
    """

    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    started_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"kite login started by {self.started_by or 'unknown'}"
