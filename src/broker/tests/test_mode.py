"""The paper/live gate.

The property under test is the one the whole design rests on: a machine that
is not the registered one cannot place a live order, and it cannot because it
fails a condition rather than because anyone recognised it as a development
machine.
"""

import pytest

from broker import mode
from broker.exceptions import LiveOrdersRefused
from configuration.models import OrderMode
from configuration.store import update

REGISTERED = "203.0.113.7"
SOMEWHERE_ELSE = "198.51.100.42"


@pytest.fixture
def outbound(monkeypatch):
    """Control what address this system appears to call from."""

    def _set(value):
        monkeypatch.setattr("broker.network.outbound_ip", lambda **_: value)

    _set(None)
    return _set


def go_live(outbound, address=REGISTERED):
    """Put the system into live mode, from the registered address."""
    outbound(address)
    update(
        changed_by="operator",
        order_mode=OrderMode.LIVE,
        registered_static_ip=address,
    )


@pytest.mark.django_db
def test_an_unconfigured_system_is_in_paper(outbound):
    assert mode.configured_mode() == OrderMode.PAPER
    assert mode.is_paper() is True


@pytest.mark.django_db
def test_paper_mode_does_not_send(outbound):
    assert mode.should_send_to_broker() is False


@pytest.mark.django_db
def test_paper_mode_refuses_live_placement(outbound):
    with pytest.raises(LiveOrdersRefused, match="configured mode is paper"):
        mode.require_live_permitted()


@pytest.mark.django_db
def test_live_from_the_registered_address_sends(live_book_open, outbound):
    go_live(outbound)
    assert mode.should_send_to_broker() is True


@pytest.mark.django_db
def test_live_is_refused_once_the_address_changes(live_book_open, outbound):
    """The elastic IP is load-bearing (D12). Losing it must stop orders, not
    send them from an address Zerodha will reject."""
    go_live(outbound)
    outbound(SOMEWHERE_ELSE)

    with pytest.raises(LiveOrdersRefused, match="not the registered"):
        mode.require_live_permitted()


@pytest.mark.django_db
def test_live_is_refused_when_the_address_is_unknown(live_book_open, outbound):
    """Unknown is never treated as a match."""
    go_live(outbound)
    outbound(None)

    with pytest.raises(LiveOrdersRefused, match="could not be determined"):
        mode.require_live_permitted()


@pytest.mark.django_db
def test_a_live_mode_with_no_registered_address_is_refused(outbound, monkeypatch):
    """Reachable only if a row were written around the store. The gate still
    holds, because placement checks the condition and not the switch."""
    from configuration.models import ConfigurationVersion

    ConfigurationVersion.objects.create(
        version=1,
        changed_by="somebody-who-bypassed-the-store",
        order_mode=OrderMode.LIVE,
        registered_static_ip=None,
    )
    outbound(REGISTERED)

    with pytest.raises(LiveOrdersRefused, match="no static IP"):
        mode.require_live_permitted()


@pytest.mark.django_db
def test_should_send_raises_rather_than_downgrading_to_paper(live_book_open, outbound):
    """An operator who believes they are live must never be quietly put back
    into paper: the portfolio would stop growing and every screen would still
    look right."""
    go_live(outbound)
    outbound(SOMEWHERE_ELSE)

    with pytest.raises(LiveOrdersRefused):
        mode.should_send_to_broker()


@pytest.mark.django_db
def test_the_permission_carries_a_reason(outbound):
    permission = mode.live_permission()
    assert bool(permission) is False
    assert "paper" in permission.reason


@pytest.mark.django_db
def test_a_granted_permission_is_truthy(live_book_open, outbound):
    go_live(outbound)
    assert bool(mode.live_permission()) is True


@pytest.mark.django_db
def test_a_refusal_is_an_error_the_alarm_hears(live_book_open, outbound, caplog):
    """A live day whose address moved places nothing, all day. The
    `task_failed` alarm reads ERROR lines from `steadystack.*`; a warning here
    was a day of refusals nobody was told about."""
    import logging

    go_live(outbound)
    outbound(SOMEWHERE_ELSE)

    with (
        caplog.at_level(logging.WARNING, logger="steadystack.broker"),
        pytest.raises(LiveOrdersRefused),
    ):
        mode.require_live_permitted()

    [line] = [r for r in caplog.records if r.getMessage() == "live placement refused"]
    assert line.levelname == "ERROR"
    assert line.name == "steadystack.broker"
