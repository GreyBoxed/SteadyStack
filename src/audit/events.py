"""The one way an audit event is written.

Going through a function rather than calling `AuditEvent.objects.create`
directly buys two things: the trace ID is stamped without anyone remembering
to, and the actor is always recorded -- an event that cannot say who caused it
answers half the question it exists to answer.

**Recording a refusal.** An event written inside the transaction that a
refusal aborts is rolled back with it, so the refusal leaves no trace at all
-- which is exactly backwards, because an attempt that was refused is often
the thing somebody needs to find later. Validate first, record the refusal,
then open the transaction that writes the change. Three modules learned this
the same way: `configuration.store`, `model_portfolio.uploads` and
`rebalance.batches`.
"""

from typing import Any

from django.db import models

from audit.context import current_trace_id
from audit.models import AuditEvent

SYSTEM_ACTOR = "system"


def record(
    action: str,
    *,
    actor: str = SYSTEM_ACTOR,
    entity: models.Model | None = None,
    entity_type: str = "",
    entity_id: str = "",
    before: Any = None,
    after: Any = None,
) -> AuditEvent:
    """Append one audit event.

    Pass `entity` for a saved model instance, or `entity_type`/`entity_id` for
    something that has no row yet -- a rejected upload, a refused mode switch.
    """
    if entity is not None:
        entity_type = entity_type or entity._meta.label
        entity_id = entity_id or str(entity.pk)

    return AuditEvent.objects.create(
        trace_id=current_trace_id(),
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
    )
