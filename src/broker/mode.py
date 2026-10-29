"""The paper/live gate.

Everything about this module is arranged so that the answer to "could this
machine place a real order by accident?" is no, for a reason that does not
depend on knowing which machine it is.

There is no environment check anywhere. A laptop cannot place a live order
because it fails a condition -- the address it calls out from is not the one
registered with Zerodha -- and not because somebody remembered to write
`if DEBUG`. The same code runs on the box in Phase 4 and places real orders
there, having changed nothing but the answer to that condition.

The gate is checked twice by design:

  * when the mode is switched to live, so the switch itself is refused
    (`configuration.store`), and
  * immediately before every placement, so an address that changed after the
    switch -- an elastic IP lost and reassigned (D12) -- stops the order
    rather than being discovered by a rejection at the broker.
"""

import logging
from dataclasses import dataclass

from audit.logging import fields
from broker import network
from broker.exceptions import LiveOrdersRefused
from configuration.models import OrderMode
from configuration.store import current

logger = logging.getLogger("steadystack.broker")


@dataclass(frozen=True)
class Permission:
    """Whether a live order may be placed, and why not when it may not."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def configured_mode() -> str:
    """The mode in configuration. Paper until somebody changes it (M19b)."""
    return current().order_mode


def is_paper() -> bool:
    return configured_mode() == OrderMode.PAPER


def live_permission() -> Permission:
    """Whether the conditions for placing a live order hold right now.

    Fails closed at every step: an unrecorded address, an undeterminable
    outbound address and a mismatch are all refusals. Only an exact match
    permits.
    """
    config = current()

    if config.order_mode != OrderMode.LIVE:
        return Permission(False, "the configured mode is paper")

    registered = config.registered_static_ip
    if not registered:
        return Permission(False, "no static IP is recorded in configuration (M12a)")

    observed = network.outbound_ip()
    if observed is None:
        return Permission(
            False,
            "the outbound address could not be determined, so it cannot be "
            "shown to match the registered one",
        )

    if observed != registered:
        return Permission(
            False,
            f"this system calls out from {observed}, not the registered {registered}",
        )

    return Permission(True)


def require_live_permitted() -> None:
    """Raise unless a live order may be placed.

    Called before the broker call, never after: the refusal must cost nothing
    and reach nobody's order book.
    """
    permission = live_permission()
    if permission:
        return

    # ERROR, so the `task_failed` alarm hears it: a refusal here is a live day
    # placing nothing, round after round, until somebody fixes the address.
    logger.error("live placement refused", extra=fields(reason=permission.reason))
    raise LiveOrdersRefused(f"Refusing to place a live order: {permission.reason}.")


def should_send_to_broker() -> bool:
    """Whether this order goes to Kite or is recorded and left unsent.

    Live *and* permitted sends. Everything else records a paper order -- which
    is a complete order in every table, not a discarded one (M19a).
    """
    if configured_mode() != OrderMode.LIVE:
        return False

    require_live_permitted()
    return True
