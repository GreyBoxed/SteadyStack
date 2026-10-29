"""The audit trail is the system's primary artefact, so it is append-only in
the database rather than by convention.

Role grants alone would not prove this: migrations and tests run as the schema
owner, which grants cannot restrain. A trigger fires for every role, so the
invariant is both enforced and testable.
"""

import pytest
from django.db import DatabaseError

from audit.models import AuditEvent, BrokerCall


@pytest.mark.django_db
def test_an_audit_event_can_be_appended():
    AuditEvent.objects.create(action="config.updated", actor="operator", trace_id="ts-2026-09-22")
    assert AuditEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_an_audit_event_cannot_be_updated():
    event = AuditEvent.objects.create(action="config.updated", actor="operator")

    with pytest.raises(DatabaseError, match="append-only"):
        AuditEvent.objects.filter(pk=event.pk).update(action="something.else")

    assert AuditEvent.objects.get(pk=event.pk).action == "config.updated"


@pytest.mark.django_db(transaction=True)
def test_an_audit_event_cannot_be_deleted():
    event = AuditEvent.objects.create(action="config.updated", actor="operator")

    with pytest.raises(DatabaseError, match="append-only"):
        AuditEvent.objects.filter(pk=event.pk).delete()

    assert AuditEvent.objects.filter(pk=event.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_a_broker_call_cannot_be_updated():
    call = BrokerCall.objects.create(endpoint="/quote", method="GET", latency_ms=12)

    with pytest.raises(DatabaseError, match="append-only"):
        BrokerCall.objects.filter(pk=call.pk).update(endpoint="/orders")

    assert BrokerCall.objects.get(pk=call.pk).endpoint == "/quote"


@pytest.mark.django_db(transaction=True)
def test_a_broker_call_cannot_be_deleted():
    call = BrokerCall.objects.create(endpoint="/quote", method="GET", latency_ms=12)

    with pytest.raises(DatabaseError, match="append-only"):
        BrokerCall.objects.filter(pk=call.pk).delete()

    assert BrokerCall.objects.filter(pk=call.pk).exists()
