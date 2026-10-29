"""The order lock, as the box runs it.

One file, shared by every process that places, cancels or settles: the timers
and the web app. Three ways it has gone wrong on paper, each guarded here
because the test suite runs on one machine and cannot see the box:

  * the settings never read `ORDER_LOCK_PATH`, so each unit -- with its own
    private /tmp -- locked a different file and excluded nothing;
  * a unit wrapped a command in `flock` on the same file the command locks
    itself, so the command found its own parent holding it and was "Busy"
    every round;
  * the lock lived in a directory the service user cannot create files in.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UNITS = ROOT / "infra" / "systemd"
BOOTSTRAP = ROOT / "infra" / "terraform" / "templates" / "bootstrap.sh.tftpl"

#: Commands that take the order lock themselves (`idempotency.once`, or the
#: operator's waiting lock): wrapping them in `flock` on the same file
#: deadlocks them against their own parent.
LOCKS_ITSELF = ("ladder_tick", "market_fallback", "settle_day", "rebalance_tick")

LOCK_DIRECTORY = "/var/lib/steadystack"


def unit_text(name: str) -> str:
    return (UNITS / name).read_text()


def exec_lines(text: str) -> list[str]:
    """Each ExecStart, with its continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line for line in joined.splitlines() if line.startswith("ExecStart=")]


def test_the_settings_read_the_lock_path_from_the_environment(monkeypatch):
    import importlib

    import config.settings.base as base

    monkeypatch.setenv("ORDER_LOCK_PATH", "/var/lib/steadystack/orders.lock")
    reloaded = importlib.reload(base)
    try:
        assert reloaded.ORDER_LOCK_PATH == "/var/lib/steadystack/orders.lock"
    finally:
        monkeypatch.delenv("ORDER_LOCK_PATH")
        importlib.reload(base)


def test_the_box_puts_the_lock_where_the_service_user_can_write():
    write_env = ROOT / "infra" / "bin" / "write-env.sh"
    env_line = re.search(r"^ORDER_LOCK_PATH=(\S+)$", write_env.read_text(), re.MULTILINE)

    assert env_line, "the box's environment has no ORDER_LOCK_PATH"
    assert env_line.group(1) == f"{LOCK_DIRECTORY}/orders.lock"


@pytest.mark.parametrize("name", sorted(p.name for p in UNITS.glob("*.service")))
def test_no_unit_wraps_a_self_locking_command_in_flock(name):
    for line in exec_lines(unit_text(name)):
        if "flock" in line:
            assert not any(command in line for command in LOCKS_ITSELF), line


@pytest.mark.parametrize("name", sorted(p.name for p in UNITS.glob("*.service")))
def test_every_unit_that_touches_the_lock_can_reach_it(name):
    text = unit_text(name)
    touches = "flock" in text or any(command in text for command in LOCKS_ITSELF)
    touches = touches or name in ("steadystack-web.service", "steadystack-ticker.service")
    if not touches:
        pytest.skip("does not touch the order lock")

    assert "StateDirectory=steadystack" in text, f"{name} cannot write {LOCK_DIRECTORY}"
    for line in exec_lines(text):
        if "flock" in line:
            assert f"{LOCK_DIRECTORY}/orders.lock" in line, line
