# Setting up SteadyStack

How to get a working installation, on a laptop or on the production box.

- **Local** (this file, part 1): a development machine that plans, places
  paper orders, fills them against the live market and settles — everything
  but a live order.
- **Production** (this file, part 2, then [`DEPLOY.md`](DEPLOY.md)): the EC2
  box. The infrastructure and first boot are in `DEPLOY.md`; the application
  setup that follows it is the same as local, run over SSM, and is in part 2.
- **Running it day to day** is [`OPERATIONS.md`](OPERATIONS.md).

The specification is `docs/superpowers/specs/2026-09-21-steadystack-prd.md`;
conventions for changing the code are in `CLAUDE.md`.

---

## Part 1 — Local

### 1.1 What you need

| Tool | Version | Why |
|---|---|---|
| Python | 3.12 or later | The application |
| [uv](https://docs.astral.sh/uv/) | any recent | Dependencies and the virtualenv (`uv.lock` is committed) |
| Docker | any recent | Postgres 16, via `docker-compose.yml` — or a local PostgreSQL 16 |
| Node.js | 20+ | **Only** to rebuild CSS after changing template classes |
| Git | any | The hooks in `.githooks/` run on commit |
| An authenticator app | — | Sign-in is password + TOTP |
| A Zerodha account with Kite Connect | — | Quotes, depth, the instrument master, holdings, the tick feed |

The Kite Connect app needs:

- the **market-data subscription** (without it nothing can be ranked or
  sized — it is a standing dependency, not an extra);
- its **redirect URL** set to `http://127.0.0.1:8000/broker/kite/callback/`
  for local use.

Zerodha does not issue Kite Connect API access on minor accounts. The
portfolio is earmarked for a child but runs in an adult's demat account.

### 1.2 Clone and install

```sh
git clone https://github.com/GreyBoxed/steadystack.git
cd steadystack
uv sync                                  # creates .venv from uv.lock, dev tools included
git config core.hooksPath .githooks      # commit-message and credential checks
```

The hooks:

- `commit-msg` validates Conventional Commits (subject ≤ 72 characters,
  scopes listed in `CLAUDE.md`);
- `pre-commit` scans staged files for credentials, then runs `ruff check` and
  `ruff format --check` on staged Python.

### 1.3 Start Postgres

```sh
docker compose up -d
docker compose ps          # wait for "healthy"
```

This starts `postgres:16-alpine` as `steadystack-postgres` on `localhost:5432`,
with a named volume `steadystack_pgdata`, and on first start runs
`ops/postgres/init/01-roles.sql`, which creates:

| Role | Password (dev only) | What it is for |
|---|---|---|
| `steadystack_owner` | `steadystack_owner_dev` | Owns the schema; runs migrations |
| `steadystack_app` | `steadystack_app_dev` | The runtime connection. Default privileges give it SELECT/INSERT/UPDATE/DELETE on what the owner creates; migrations then **revoke UPDATE and DELETE** on the append-only tables (cash ledger, configuration, audit events and broker calls, realised gains, corrections, journal entries and lines, period closes, statement uploads, balance checks), and **DELETE only** on lots and statement lines, whose triggers allow the one change each may take |

The init script only runs against an empty volume. To start again from
nothing: `docker compose down -v && docker compose up -d`.

Using a local PostgreSQL instead: create the database `steadystack` owned by
`steadystack_owner`, then run `01-roles.sql` against it as a superuser.

### 1.4 Configure `.env`

```sh
cp .env.example .env
```

| Variable | Local value | Notes |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` | `config.settings.prod` refuses DEBUG and forces HTTPS |
| `DJANGO_SECRET_KEY` | any long random string | `python -c "import secrets;print(secrets.token_urlsafe(64))"` |
| `DJANGO_DEBUG` | `true` | Required for `seed_demo` |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `DATABASE_URL` | `postgres://steadystack_app:steadystack_app_dev@localhost:5432/steadystack` | The restricted runtime role |
| `DATABASE_MIGRATION_URL` | `postgres://steadystack_owner:steadystack_owner_dev@localhost:5432/steadystack` | Used only by `migrate` (the `migration` alias) |
| `KITE_API_KEY` | from your Kite Connect app | |
| `KITE_API_SECRET` | from your Kite Connect app | Also verifies postback checksums |
| `KITE_TOKEN_KEY` | a Fernet key | Encrypts the daily access token at rest; no key, no stored session |

Generate the Fernet key:

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env` is gitignored. **Never commit it.** A leaked Kite key or TOTP seed is
fixed by rotating it at Zerodha / re-enrolling, not by deleting the commit.

### 1.5 Create the schema and your sign-in

```sh
uv run python manage.py migrate                    # uses DATABASE_MIGRATION_URL automatically
uv run python manage.py createsuperuser            # your username and password
uv run python manage.py enrol_totp <username>      # prints a QR code and ten recovery codes
```

- A permission error during `migrate` means the `migration` alias is missing
  (`DATABASE_MIGRATION_URL` unset), not that the migration is wrong.
- Scan the QR code into the authenticator app. Keep the recovery codes
  somewhere other than this laptop.
- A new phone later: `enrol_totp <username> --name "new phone" --replace`,
  or from the browser under **Account → Security**.

### 1.6 Load the reference data

```sh
uv run python manage.py load_calendar ops/calendar/nse-2026.csv
uv run python manage.py check_calendar          # how far ahead sessions are loaded
```

The calendar CSV is `date,kind,open_time,close_time,note`. It records
*sessions*, not holidays, so a special session (Muhurat) is a row with its own
hours; weekends never trade whatever a row says. Load next year's file before
December runs out — `preflight` warns when coverage is low.

### 1.7 First run in the browser

```sh
uv run python manage.py runserver
```

`runserver` prints the state of the installation before it serves:
database, migrations, order mode, Kite session, model portfolio, calendar,
and the times the day acts on. A `!` line stops a trading day; a `~` line is
worth knowing and will not. `uv run python manage.py preflight` prints the
same report on its own and exits non-zero while anything blocks.

At <http://127.0.0.1:8000>:

1. **Sign in** with the password and an authenticator code.
2. **Settings → Kite session → Connect** (`/broker/session/`). You log in at
   Zerodha and come back to `/broker/kite/callback/`; the access token is
   stored encrypted and lasts until about 06:00 the next morning. This is a
   **daily** step.
3. **Instruments**: fill the master from Kite (needs the session):

   ```sh
   uv run python manage.py refresh_instruments
   ```

   Only tradable NSE equities and ETFs are kept (series EQ/BE, lot size 1);
   indices are kept separately for the benchmark. Run it again whenever a new
   listing matters.
4. **Settings → Model portfolio** (`/model-portfolio/`): upload a CSV of
   `symbol,target_pct` summing to 100 (see
   `ops/model-portfolio/2026-09-23-long-hold-v1.csv`), review the diff, store
   it. Weights are versioned; a past tradesheet stays explicable against the
   weights of its day.
5. **Settings → Configuration** (`/configuration/`): daily amount (default
   ₹1,000), the clock, pricing, the rate limit. Every save is a new audited
   version with a required reason. **Order mode stays `paper`.**
6. **Existing holdings**, if you already own shares, once:

   ```sh
   uv run python manage.py import_opening_positions holdings.csv \
       --actor <you> --source "Zerodha holdings statement" --dry-run
   # then again without --dry-run
   ```

   The CSV is `symbol,quantity,acquired_on,cost_per_unit`; each row becomes an
   immutable FIFO lot. `--partial` leaves the import open for more files.

   If the account held nothing, say so instead -- gains and rebalance sells
   stay refused until the import is complete either way:

   ```sh
   uv run python manage.py import_opening_positions --empty \
       --actor <you> --source "Zerodha holdings statement: no holdings"
   ```

### 1.8 Run a trading day on a laptop

There is no scheduler on macOS; systemd is the production box. A day is run by
hand while the market is open (09:15–15:30 IST, trading days only). Planning
*today* outside market hours is refused.

Four things stay running together — Postgres, `runserver` (for the browser),
`run_ticker` and `run_day.sh --watch`. The README's
[*What has to be running*](README.md#what-has-to-be-running) table says what
each is for and what breaks without it.

**Terminal 1 — the tick feed.** Paper orders fill against it; without it
nothing fills.

```sh
uv run python manage.py run_ticker
uv run python manage.py run_ticker --once     # just check the feed connects
```

**Terminal 2 — the day.**

```sh
./ops/local/run_day.sh             # preflight, session check, instruments, plan, authorize prompt, one round
./ops/local/run_day.sh --watch     # a placing round every 30s to 14:00, the close's rounds 15:00–15:25, then settle
./ops/local/run_day.sh --settle    # close a day that already ran: close round, settle, valuation
./ops/local/run_day.sh --date 2026-09-21 --settle
./ops/local/run_day.sh --dry-run   # print the steps without running them
```

`--watch` can be started at any time of day; a day that is already placing
keeps its plan and carries on from where the clock says it is.

The same steps from the browser, on **Today**: *Generate today's tradesheet*,
*Authorize* (nothing is placed until you do), *Regenerate* (until the first
order is out — e.g. after crediting a lumpsum under **Ledger → Cash**).

Or one command at a time:

```sh
uv run python manage.py check_kite_session
uv run python manage.py generate_tradesheet               # --allow-closed-market for testing outside hours
uv run python manage.py authorize_tradesheet --actor <you>
uv run python manage.py ladder_tick                        # one placing round; its number comes from the clock
uv run python manage.py market_fallback                    # one round of the close
uv run python manage.py settle_day
uv run python manage.py snapshot_valuation
```

A day cannot be compressed: round numbers come from the wall clock and a round
that already ran is refused, not repeated.

### 1.9 A database to look around in

```sh
uv run python manage.py seed_demo --reset          # --days 250 by default
```

Fills a development database with a believable history through the real code
paths. It refuses unless `DEBUG` is on and the mode is paper, and `--reset`
**deletes what is there** — never run it against data you want to keep.

### 1.10 Tests, lint, CSS

```sh
uv run pytest                                   # ~1,340 tests, about two minutes; needs Postgres
uv run pytest src/orders -q                     # one app
uv run ruff check src && uv run ruff format --check src
```

- Tests use a throwaway `test_steadystack` database. **One run at a time** — two
  runs share it and fail each other.
- CSS is Tailwind v4, built by the CLI from `src/static/css/input.css`. After
  changing any template's classes:

  ```sh
  npm install        # once
  npm run build      # writes src/static/css/app.css, which IS committed
  ```

  The box never runs Node; it serves the committed `app.css`.

### 1.11 Local troubleshooting

| Symptom | Cause and fix |
|---|---|
| `migrate` fails with a permission error | `DATABASE_MIGRATION_URL` is missing from `.env` |
| `migrate` hangs | Another process holds a lock on the table — a stuck `runserver`/shell. Stop it and retry |
| Every page redirects to sign-in after a password | TOTP not enrolled: `enrol_totp <username>` |
| "No Kite session" everywhere | The token expired overnight. **Settings → Kite session → Connect** |
| `git commit` blocked by the Xcode licence | Run `sudo xcodebuild -license accept` in a real terminal |
| Commit rejected by the credential scan | Read what it found. If it is a false positive, `SKIP_SECRET_SCAN=1 git commit ...`; if it is real, remove it and rotate it |
| Paper orders never fill | `run_ticker` is not running, or the name is not subscribed — it resubscribes every 30 s |
| Styles look stale | Hard refresh; the stylesheet URL carries a build stamp |
| Two test runs error on the database | Only one `pytest` at a time |

---

## Part 2 — Production

The box is one EC2 instance in Mumbai (ap-south-1) running nginx, gunicorn,
Django, the tick daemon, a local PostgreSQL 16 and systemd timers, all from
`infra/`. **It has not been applied yet**; the steps outside the repository
are in [`DEPLOY.md` §2](DEPLOY.md#2-before-the-first-apply-what-is-not-code).

The order of operations:

1. Provision and first boot — [`DEPLOY.md` §3–§5](DEPLOY.md#3-provision).
2. Application setup, below.
3. Paper days in production — [`DEPLOY.md` §7](DEPLOY.md#7-paper-in-production).
4. Going live — [`DEPLOY.md` §8](DEPLOY.md#8-going-live).

### 2.1 Getting a shell

There is no SSH and no key pair. Shell access is SSM Session Manager:

```sh
cd infra/terraform
terraform output shell                     # prints the whole command
aws ssm start-session --region ap-south-1 --target i-XXXXXXXX
```

On the box, run management commands as the app user with the environment the
services use:

```sh
sudo -u steadystack bash -c 'cd /opt/steadystack && set -a && . /run/steadystack/env && set +a && .venv/bin/python manage.py preflight'
```

A helper saves typing (put it in the root user's shell profile; it is not
installed by bootstrap):

```sh
tx() { sudo -u steadystack bash -c "cd /opt/steadystack && set -a && . /run/steadystack/env && set +a && .venv/bin/python manage.py $*"; }
tx preflight

# migrate only: the schema owner's URL is in root's owner.env, never in the
# environment the services or `tx` read.
txm() { sudo bash -c "cd /opt/steadystack && set -a && . /run/steadystack/env && . /run/steadystack/owner.env && set +a && sudo -E -H -u steadystack .venv/bin/python manage.py $*"; }
```

### 2.2 Application setup on the box

The same steps as local §1.5–§1.7, without Docker and without `.env` (the
environment is `/run/steadystack/env`, written from Secrets Manager at boot):

```sh
txm migrate                        # bootstrap already ran it if the secrets were real at boot
tx createsuperuser
tx enrol_totp <username>           # scan the QR from the terminal; keep the recovery codes off the box
tx load_calendar ops/calendar/nse-2026.csv
tx check_calendar
```

Then in the browser at `https://<domain>`:

1. Sign in, **Settings → Kite session → Connect**. The Kite app's redirect
   URL must be `https://<domain>/broker/kite/callback/` for this host — either
   change it on the app or use a second Kite app for production.
2. `tx refresh_instruments`
3. Upload the model portfolio, set the configuration (daily amount, clock),
   **leave order mode on paper**.
4. `tx import_opening_positions <csv> --actor <you> --source "..."` if there
   are existing holdings, or `tx import_opening_positions --empty --actor <you>
   --source "..."` if there are none. Copy the CSV up with `aws s3 cp` through a bucket you
   control, or paste it via the SSM shell — there is no SCP.
5. `tx preflight` — nothing may be marked `!`.

The timers then run the day by themselves; see [`OPERATIONS.md`](OPERATIONS.md).
