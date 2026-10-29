"""The broker boundary.

Nothing outside this package talks to Kite. Everything inside it is arranged
so that `place_order` is the only call that can be refused for a reason that
has nothing to do with the broker.
"""

from broker.base import Broker


def get_broker(*, attach: bool = True, **kwargs) -> Broker:
    """The Kite client, carrying today's session.

    There is one implementation and it is the real one, in every environment.
    What varies is whether an order is sent (`broker.mode`), never which
    client is constructed -- so no code is ever exercised against a stand-in
    that behaves better than Kite does.

    The stored token is applied here rather than by each caller. Leaving it to
    the caller cost a dashboard that reported a portfolio worth nothing, a
    tradesheet that could not price the ask and an evening valuation that was
    never written -- all of them reporting "Invalid `api_key` or
    `access_token`" while a live session sat in the table. Fourteen call sites
    had to remember one line and four did.

    Attaching is a no-op when there is no live session, so the login flow --
    which asks for a client precisely because it has no token yet -- is
    unaffected. `attach=False` is for a caller that wants the unauthenticated
    client deliberately.
    """
    from broker.kite import KiteBroker

    broker = KiteBroker(**kwargs)

    if attach and kwargs.get("access_token") is None:
        from broker import session

        session.attach(broker)

    return broker


__all__ = ["Broker", "get_broker"]
