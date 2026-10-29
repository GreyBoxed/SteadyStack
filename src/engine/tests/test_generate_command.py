"""The 09:45 plan failing, and the line its alarm matches.

The `task_failed` alarm in infra/terraform/observability.tf matches
`$.level = "ERROR" && $.logger = "steadystack.*"`. A refused plan used to leave
only a CommandError on stderr -- which the log shipper never reads as a
structured line -- so a day that never planned paged nobody.
"""

import json
import logging

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from audit.logging import JSONFormatter


@pytest.fixture
def planning_fails(monkeypatch):
    from engine.planning import PlanningFailed

    def refuse(**_kwargs):
        raise PlanningFailed("Kite could not be read")

    monkeypatch.setattr("engine.planning.plan_from_broker", refuse)


@pytest.mark.django_db
def test_a_failed_plan_logs_the_error_the_alarm_matches(planning_fails, caplog):
    with (
        caplog.at_level(logging.ERROR, logger="steadystack.engine"),
        pytest.raises(CommandError, match="Kite could not be read"),
    ):
        call_command("generate_tradesheet", "--date", "2026-09-22")

    [line] = [r for r in caplog.records if r.name == "steadystack.engine"]
    body = json.loads(JSONFormatter().format(line))
    assert body["level"] == "ERROR"
    assert body["logger"] == "steadystack.engine"
    assert body["message"] == "tradesheet generation failed"
    assert body["error"] == "Kite could not be read"
