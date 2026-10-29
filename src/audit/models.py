"""Append-only records.

Neither model is ever updated or deleted. Enforcement is a database trigger
(see the append_only migration), not application discipline -- these rows are
the permanent explanation for why money moved, and an ORM call that could
rewrite them is a liability.
"""

import uuid

from django.db import models


class AuditEvent(models.Model):
    """A state transition or a configuration change, with who and what."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    actor = models.CharField(max_length=150, help_text="username, or 'system'")
    action = models.CharField(max_length=100, db_index=True)

    entity_type = models.CharField(max_length=100, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)

    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor} at {self.occurred_at:%Y-%m-%d %H:%M:%S}"


class BrokerCall(models.Model):
    """Every request to the broker and what came back.

    When the broker's record and ours disagree, this table is the evidence.
    Credentials are redacted before the row is written, never after.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)

    endpoint = models.CharField(max_length=200, db_index=True)
    method = models.CharField(max_length=10)

    request = models.JSONField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)

    status_code = models.IntegerField(null=True, blank=True)
    latency_ms = models.IntegerField()
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-occurred_at"]

    def __str__(self) -> str:
        return f"{self.method} {self.endpoint} -> {self.status_code or self.error}"
