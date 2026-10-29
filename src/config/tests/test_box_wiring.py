"""The box, read as text.

Nothing here can run systemd, nginx or Terraform, so what is checked is what
text can prove: that the scripts parse, and that the pieces name each other
the way the box needs them to. Each was a first-boot failure on paper.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "infra"
UNITS = INFRA / "systemd"
BOOTSTRAP = INFRA / "terraform" / "templates" / "bootstrap.sh.tftpl"

bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(bash is None, reason="no bash on this machine")


def render(template: str) -> str:
    """Terraform's templatefile, as far as this template uses it: `${name}`
    is a variable, and `$${` is a literal `${`."""
    rendered = re.sub(r"(?<!\$)\$\{([a-z_]+)\}", lambda m: f"placeholder-{m.group(1)}", template)
    return rendered.replace("$${", "${")


@needs_bash
@pytest.mark.parametrize(
    "script",
    ["infra/bin/write-env.sh", "infra/bin/backup.sh", "ops/local/run_day.sh"],
)
def test_the_scripts_parse(script):
    subprocess.run([bash, "-n", str(ROOT / script)], check=True)  # noqa: S603


@needs_bash
def test_bootstrap_parses_once_rendered(tmp_path):
    rendered = tmp_path / "bootstrap.sh"
    rendered.write_text(render(BOOTSTRAP.read_text()))

    subprocess.run([bash, "-n", str(rendered)], check=True)  # noqa: S603


def test_the_scripts_the_box_runs_are_executable():
    for script in ("write-env.sh", "backup.sh"):
        assert (INFRA / "bin" / script).stat().st_mode & 0o111, script


def test_every_unit_with_the_environment_waits_for_it_to_be_written():
    for unit in UNITS.glob("*.service"):
        text = unit.read_text()
        if "EnvironmentFile=/run/steadystack/env" not in text:
            continue
        assert "Requires=steadystack-env.service" in text, unit.name
        assert "After=steadystack-env.service" in text, unit.name


def test_bootstrap_enables_the_environment_unit_and_writes_its_inputs():
    text = BOOTSTRAP.read_text()

    assert "systemctl enable steadystack-env.service" in text
    assert "/etc/steadystack/bootstrap.env" in text
    assert "infra/bin/write-env.sh" in text


def test_the_log_directory_exists_before_anything_starts():
    text = BOOTSTRAP.read_text()

    made = text.index("/var/log/steadystack\n")
    assert made < text.index("systemctl restart steadystack-web")
    assert made < text.index("systemctl restart nginx")


def test_the_web_process_never_holds_the_schema_owner():
    write_env = (INFRA / "bin" / "write-env.sh").read_text()
    runtime = write_env.split('write "$ENV_FILE"', 1)[1].split("EOF", 2)[1]

    assert "DATABASE_MIGRATION_URL" not in runtime
    assert "owner.env" not in (UNITS / "steadystack-web.service").read_text()
    assert "EnvironmentFile=/run/steadystack/owner.env" in (UNITS / "backup.service").read_text()


def test_gunicorn_can_import_the_project():
    """`config` is under src/, which manage.py adds for itself."""
    assert "--pythonpath /opt/steadystack/src" in (UNITS / "steadystack-web.service").read_text()


def test_the_evening_records_a_valuation_after_it_settles():
    text = (UNITS / "eod.service").read_text()

    assert text.index("settle_day") < text.index("snapshot_valuation")


@pytest.mark.parametrize("timer", sorted(p.name for p in UNITS.glob("*.timer")))
def test_bootstrap_enables_every_timer(timer):
    """A timer installed but never enabled is a schedule that never runs."""
    joined = BOOTSTRAP.read_text().replace("\\\n", " ")
    enabled = {
        word
        for line in joined.splitlines()
        if line.strip().startswith("systemctl enable --now")
        for word in line.split()
    }

    assert timer in enabled


def test_an_unsettled_day_is_retried_through_the_evening():
    """B2: Kite's trade book holds today's trades only, so a live day must
    settle before midnight; the retry covers the evening, never past it."""
    timer = (UNITS / "settle-retry.timer").read_text()
    service = (UNITS / "settle-retry.service").read_text()

    for when in ("16:15", "17:30", "20:00", "22:30"):
        assert f"*-*-* {when} Asia/Kolkata" in timer
    assert "Persistent=false" in timer
    assert "settle_day --if-unsettled" in service
    assert service.index("settle_day") < service.index("snapshot_valuation")


def test_the_morning_refreshes_the_instrument_master_before_it_plans():
    text = (UNITS / "tradesheet.service").read_text()

    assert text.index("refresh_instruments") < text.index("generate_tradesheet")


def test_nginx_names_a_certificate_only_in_the_config_installed_once_one_exists():
    assert "ssl_certificate" in (INFRA / "nginx" / "steadystack.conf").read_text()
    assert "443" not in (INFRA / "nginx" / "steadystack-http.conf").read_text()


def test_every_log_file_is_shipped_on_its_own():
    """The agent follows one file per wildcard; each unit's log is named."""
    text = BOOTSTRAP.read_text()
    shipped = set(re.search(r"for name in ([^;]+); do", text).group(1).split())

    for unit in UNITS.glob("*.service"):
        for log in re.findall(r"append:/var/log/steadystack/([\w-]+)\.log", unit.read_text()):
            assert log in shipped, f"{unit.name} writes {log}.log, which is never shipped"


def _going_live() -> str:
    deploy = (ROOT / "DEPLOY.md").read_text()
    return deploy.split("## 8. Going live", 1)[1]


def test_deploy_recreate_step_grants_before_migrating():
    """NB1: default privileges live in the database that was dropped. The
    recreated one needs bootstrap's grant block before `txm migrate`, so the
    migrations' REVOKEs narrow real grants; granting after the tables exist
    either grants nothing or hands UPDATE back on the append-only tables."""
    section = _going_live()
    step2 = section.split("2. **Recreate the database.**", 1)[1].split("3. **", 1)[0]

    grants = (
        "GRANT CONNECT ON DATABASE steadystack TO steadystack_app;",
        "GRANT USAGE ON SCHEMA public TO steadystack_app;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO steadystack_app;",
        "GRANT USAGE, SELECT ON SEQUENCES TO steadystack_app;",
    )
    for grant in grants:
        assert grant in step2, grant
        assert step2.index(grant) < step2.index("txm migrate"), grant
    assert "rerun bootstrap" not in step2.lower()
    # And the same four lines are what bootstrap runs, so the two cannot drift.
    bootstrap = BOOTSTRAP.read_text()
    for grant in grants:
        assert grant in bootstrap, grant

    step7 = section.split("7. **", 1)[1].split("8. **", 1)[0]
    assert "steadystack_app" in step7

    operations = (ROOT / "OPERATIONS.md").read_text().split("## 9. Backups and restore", 1)[1]
    restore = operations.split("**Real restore**", 1)[1].split("## 10.", 1)[0]
    assert restore.index("ALTER DEFAULT PRIVILEGES") < restore.index("pg_restore --no-owner --role")
    assert "rerun bootstrap for the default grants" not in restore


def test_the_close_is_not_caught_up_after_a_boot():
    """NI3: a caught-up 15:45 settle on a morning boot settles *today*,
    before it is planned. The boot unit settles the previous day instead."""
    assert "Persistent=false" in (UNITS / "eod.timer").read_text()
    assert "Persistent=true" not in (UNITS / "eod.timer").read_text()


def test_a_boot_settles_the_previous_day_if_it_is_unsettled():
    unit = (UNITS / "settle-catchup.service").read_text()

    assert "settle_day --previous --if-unsettled" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "After=postgresql.service" in unit
    assert "Requires=steadystack-env.service" in unit
    joined = BOOTSTRAP.read_text().replace("\\\n", " ")
    enabled = [
        line
        for line in joined.splitlines()
        if line.strip().startswith("systemctl enable") and "settle-catchup.service" in line
    ]
    assert enabled
    # Enabled for the next boot, not started now: bootstrap runs on a box
    # that has no day to catch up.
    assert all("--now" not in line for line in enabled)


def test_the_boot_catch_up_never_levels_unseen_orders():
    """I-1: levelling a past live day's open orders without its tradebook
    is an operator's call, made after reading Console -- never a boot's."""
    for path in UNITS.iterdir():
        commands = [line for line in path.read_text().splitlines() if line.startswith("ExecStart")]
        assert not any("--level-unseen" in line for line in commands), path.name
    catch_up = (UNITS / "settle-catchup.service").read_text()
    assert "manage.py settle_day --previous --if-unsettled\n" in catch_up


def test_operations_names_retry_with_level_unseen_after_a_failed_settle():
    """m-3: a failed 15:45 settle leaves its claim; the documented route
    releases it in the same step."""
    operations = (ROOT / "OPERATIONS.md").read_text()
    section = operations.split("## 7. When something goes wrong", 1)[1].split("## 8.", 1)[0]
    assert "settle_day --date <day> --retry --level-unseen --actor <you>" in section


def test_deploy_step_7_requires_no_unsettled_history_line():
    """Minor 4 (wave H): preflight's new `~` line names an old tradesheet
    with no ledger row; step 7 must say the switch waits for it to be
    clean, not only for the runtime-role row."""
    step7 = _going_live().split("7. **`tx preflight`**", 1)[1].split("8. **", 1)[0]
    assert "settlement history" in step7


def test_operations_names_the_exchange_id_command():
    """Minor 3 (wave H): the §7 row that tells an operator two candidate
    orders could take a trade names the audited command that records the
    exchange number, not a bare pointer at the field."""
    operations = (ROOT / "OPERATIONS.md").read_text()
    section = operations.split("## 7. When something goes wrong", 1)[1].split("## 8.", 1)[0]
    assert "record_exchange_order_id --order" in section
    assert "--exchange-id" in section and "--actor" in section


def _verification_rows() -> dict[int, str]:
    table = _going_live().split("**Go-live verification.**", 1)[1].split("\n\n", 2)[1]
    rows = {}
    for line in table.splitlines():
        match = re.match(r"\| (\d+) \|", line)
        if match:
            rows[int(match.group(1))] = line
    return rows


def test_the_go_live_verification_list_covers_what_only_the_account_proves():
    """NI4, Nm3, Nm6: the rows the round-2 review found missing or short."""
    rows = _verification_rows()

    assert sorted(rows) == list(range(1, 14))
    assert "live_balance" in rows[1] and "planning.py" in rows[1]
    assert "per scrip" in rows[7] and "stamp" in rows[7]
    assert "SELECT 1 FROM journal_journalentry" in rows[10]
    assert "import_tradebook" in rows[11] and "--dry-run" in rows[11]
    assert "exchange" in rows[11]
    assert "steadystack_app" in rows[12] and "step 2" in rows[12]
    assert "order_history" in rows[13]


# -- go-live round 3 (m-5, Nm2, Nm5, Nm7) ------------------------------------


def test_the_recreate_step_sets_up_in_setups_order():
    """Nm2: the user table was dropped, so enrol_totp needs createsuperuser
    first; and SETUP §2.2 logs in to Kite before refresh_instruments."""
    step2 = _going_live().split("2. **Recreate the database.**", 1)[1].split("3. **", 1)[0]

    order = ["createsuperuser", "enrol_totp", "Kite session", "refresh_instruments"]
    positions = [step2.index(name) for name in order]
    assert positions == sorted(positions), order


def test_shipping_code_enables_a_new_unit():
    """Nm7: installing a unit file does not enable it."""
    deploy = (ROOT / "DEPLOY.md").read_text()
    shipping = deploy.split("## 6. Shipping code", 1)[1].split("### 6.1", 1)[0]

    assert "systemctl enable" in shipping
    assert "settle-catchup.service" in shipping and "settle-retry.timer" in shipping
    assert "bootstrap" in shipping


def test_the_opening_window_says_what_earlier_money_is():
    """Nm5: money on or before --on is float inside the opening; only money
    after it is a lumpsum, recorded after the switch."""
    step6 = _going_live().split("6. **Open the books**", 1)[1].split("7. **", 1)[0]

    assert "float" in step6
    assert "after `--on`" in step6
    assert "after the switch" in step6


def test_going_back_to_paper_says_how_real_share_movements_are_recorded():
    """m-5: classify is refused in paper, so a real movement made in a paper
    stint is classified from the first live evening's divergence."""
    section = _going_live()
    back = section.split("To go back to paper", 1)[1]
    assert "first live evening" in back and "dated when" in back
    live_sells = section.split("Live rebalance sells are not part of go-live", 1)[1]
    live_sells = live_sells.split("**Go-live verification.**", 1)[0]
    assert "classify" in live_sells.lower() and "divergence" in live_sells


def test_verification_item_two_checks_the_exchange_number_early():
    """I-1's fallback matches by exchange number: it must be on the first
    live day's orders after the first round, not only after the settle."""
    row = _verification_rows()[2]
    assert "exchange_order_id" in row and "first round" in row
