"""The session page's 09:00 check, read from the sessions table."""

from datetime import datetime, time

import pytest
import time_machine
from django.urls import reverse
from django.utils import timezone

from audit.context import today_ist


@pytest.mark.django_db
def test_the_session_page_reads_the_nine_oclock_check_from_stored_sessions(signed_in):
    nine = timezone.make_aware(datetime.combine(today_ist(), time(9, 0)))
    with time_machine.travel(nine.replace(hour=10), tick=False):
        response = signed_in.get(reverse("broker:session"))

    assert response.context["morning"]["state"] == "failed"
    assert response.context["blocked"] > 0
    assert b"PAPER" in response.content
