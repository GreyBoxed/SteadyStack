# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`steadystack` is a long-horizon equity accumulation system: it invests a fixed
rupee amount into Indian stocks every trading day, through the Zerodha Kite
Connect API, against a configured set of target weights.

The portfolio is earmarked for a child but **runs in an adult's demat
account** — Zerodha does not issue Kite Connect API access on minor accounts.
Do not write code or docs that assume a minor account.

The full specification lives in `docs/superpowers/specs/`. That PRD is the
source of truth for behaviour; this file covers conventions only.

## Status

Phases 1, 2 and 3 of `docs/plans/` are built. The system plans a day, places a
sliced ladder of orders, fills them against the live tick feed in paper mode,
settles the ledger, keeps FIFO lots, reconciles itself against the broker, and
reports what all of that came to. Phase 4 is the box, the registered address
and the first live order.

The `performance` app holds every reported number -- gains, XIRR against the
benchmark, the daily value series, the two ledger surfaces -- and the `web` app
only renders them. A view that computes a gain is in the wrong file.

The live Kite reads are verified against the real API from a laptop:
instrument master, quotes, depth, holdings, positions, margins, the order book
and the daily login. No order has ever been sent — mode is paper, and live is
refused unless the registered static IP matches.

The charge rates in `portfolio/charges.py` were taken from the broker's
published schedule and confirmed against a real contract note on 22 September
2026. They still move -- with a Union Budget, an exchange circular, a GST
change -- and the tests that pin them are how that gets noticed.

The Kite market-data subscription is active and permanent on the current API
key. Quotes, depth and the instrument master all work; without it the engine
cannot rank or size, so it is a standing dependency rather than an optional
extra.

- Django, server-side rendered (no SPA)
- Username + password + TOTP authentication
- Postgres on a single EC2 box, provisioned with Terraform — **not yet built**
- systemd timers drive the schedule; there is no Celery and no Redis

Two connections to one database: `DATABASE_URL` is the restricted runtime role
and `DATABASE_MIGRATION_URL` the schema owner. `manage.py migrate` uses the
owner's automatically; a permission error there means the alias is missing.

## Core invariants

These hold across the whole system. Breaking one is a bug even if tests pass.

- **Every movement of money is a balanced, append-only journal entry.**
  Debits equal credits in every entry and the database refuses one that does
  not; nothing is edited, a mistake is reversed by a new entry, and a closed
  financial year accepts nothing dated inside it. Carry is the balance of the
  engine's cash accounts. The daily `CashLedger` row is a summary of the
  engine's book, not the books.
- **Scheduled tasks are idempotent.** A tick that runs twice must not buy
  twice. Guarded by `flock` plus a unique constraint on
  `(trading_date, task, stream, tick_no)`, where `stream` separates the daily
  buy from each rebalance batch.
- **Lots are FIFO and immutable.** The entity is `Lot`. A fill creates one; a
  sell consumes lots oldest first and writes an immutable realised-gain row.
  Never mutate a consumed lot. FIFO is kept because the broker matches that
  way and two records that agree are worth more than two that don't — not
  because tax is being modelled. Nothing tax-specific is built here.
- **Orders are live or paper, and paper is the default.** A paper order is
  recorded exactly as a live one — same tables, same state machine, same audit
  events, marked paper — and is simply never sent; it fills against the live
  tick feed. Live is refused unless the registered static IP is recorded and
  matches the address the system is calling from, and the engine refuses
  before any broker call (M19a–M19c, M12a).
- **Nothing is placed until a person authorizes the day.** Tradesheets are
  planned automatically — the 09:45 timer, Generate, Regenerate — but the
  ladder and the 15:00 fallback place nothing for the daily buy until the
  latest version is authorized (M15f). A regenerated or replanned version
  needs authorizing again. Today's sheet is only planned while the market is
  in session (M15g).
- **Every broker call is persisted** to the `BrokerCall` table with its
  request, response, latency and status, API credentials redacted.
- **Every trading day carries a `trace_id`** (`ts-YYYY-MM-DD`) stamped on
  every log line, audit event and broker call for that day.

## Styling

Tailwind v4, built by the CLI. The design tokens live in `@theme` in
`src/static/css/input.css` -- ground, panel, card, the ink ramp, accent, good
and bad -- and every utility comes from them, so a retheme is that block.
Bare HTML carries the look through `@layer base`, because these pages are
dense records and a table should read as a table in the markup.

**After changing any template's classes, run `npm run build`.** The output
`src/static/css/app.css` is committed on purpose: the box then needs no Node
and no network at deploy time. `node_modules/` is not committed, and nothing
at runtime depends on it.

The phone is its own five screens under `/m/` (`web/phone.py`, templates in
`phone/`): sign-in, Today, authorise, Kite session and stop today. They post
to the engine's own views with a `next` back to the phone, so no rule has a
second copy. `web/device.py` sends a phone's GET of `/`, sign-in or the Kite
page to its `/m/` counterpart and a desk's the other way; every other page
opens as it is on any device. The `site` cookie, set by "Desktop site" /
"phone site", overrules the guess.

The screens are designed in a Claude Design canvas (15 artboards), exported
to `docs/design/`. The canvas is the reference for layout and copy; where it and
the PRD disagree -- it carries tax and holding-period surfaces that decision
16 withdrew -- the PRD wins until that is settled.

## Running it locally

`manage.py runserver` prints the state of the installation before it serves --
database, migrations, order mode, Kite session, model portfolio, calendar and
the times the day will act on -- and then how to run a day, with anything
blocking listed first and linked at the address the server actually bound to.
A `!` stops a trading day; a `~` is worth knowing and will not.
`manage.py preflight` is the same report on its own, and exits non-zero when
something blocks.

There is no scheduler on a laptop -- systemd is Phase 4 and does not exist on
macOS -- so `ops/local/run_day.sh` walks a day by hand: preflight, session
check, tradesheet, an authorize prompt, a round, then `--watch` to run a round
every thirty seconds until the window closes and the close's rounds after
15:00, or `--settle` to close a day that already ran. The
same steps exist in the browser: Today offers Generate (no sheet yet),
Authorize and Regenerate, and `manage.py authorize_tradesheet --actor NAME`
authorizes from a terminal. Planning today outside market hours is refused;
`generate_tradesheet --allow-closed-market` overrides that for testing. A day
that has bought is never regenerated; stop it and **Replan the rest** (or
`manage.py replan_tradesheet --reason ...`), which is paper only for now.

It cannot compress a day. `ladder_tick` takes its round number from the wall
clock, which is what lets a parked day resume where the time implies, so sixty
rounds in a row would all claim the same round and the idempotency guard would
reject fifty-nine. `market_fallback` is the same, one close round per call.

`manage.py seed_demo --reset` fills a development database through the real
code paths. It refuses unless DEBUG is on and the mode is paper.

`manage.py open_books --cash ... --on ... --actor ...` posts the live book's
opening entry once, before go-live (DEPLOY.md §8): `--on` is the last trading
day before go-live and `--cash` that day's closing funds balance; only the
difference from the cash the live book already records posts. Until then the
09:05 balance check and the funding warnings stay silent, and the switch to
live is refused. After that, the monthly Console funds statement is uploaded
at `/books/statement/`.

## Commit conventions

This project uses Conventional Commits v1.0.0, validated by
`.githooks/commit-msg`.

Run `/atomic:atomic-commit` to auto-group and commit changes. Do not use raw
`git commit`.

**Scopes**:

- `engine` — tradesheet generation, weight-gap ranking, carry arithmetic
- `orders` — order state machine, placement ladder, market fallback
- `kite` — broker client, session lifecycle, postbacks, tick websocket
- `portfolio` — holdings, lots, valuation
- `gains` — FIFO matching, realised and unrealised gains; no tax reporting
- `journal` — the double-entry journal, statement import, balance checks, the
  books screens
- `web` — views, templates, dashboards
- `config` — configuration pages, target weights, rebalance-sell upload
- `accounts` — authentication, TOTP
- `audit` — structured logging, audit events, broker-call records
- `infra` — Terraform, systemd units, deployment
- `db` — migrations

**Domain vocabulary**:

- **tradesheet** — the day's buy decision: ranking snapshot, one line per
  name the cash reaches (each up to its target), and the cash it may spend
- **replan** — a new version of a day that has bought and been stopped, net
  of its fills: ranked against what the day bought, sized from what it has
  left, settled into the same ledger row. Not a regeneration, which a day
  that has bought never gets
- **gap** — a holding's current weight minus its target weight; the most
  negative gap is the most underweight
- **carry** — the engine cash balance (1110 + 1200): budget allocated and
  not yet spent. It may go negative after an overspend; the next allocation
  repays it
- **allocation** — moving money from unallocated to the engine: the daily
  amount, a lumpsum, reinvested proceeds, restored carry
- **book** — live or paper; real money is always live
- **sweep** — SEBI's quarterly settlement, which returns the balance to the
  owner's bank and leaves carry owed
- **ladder** — the placement loop, run in thirty-second *rounds*. Each name
  has an even schedule across the window; a round places a *new* limit order
  for what a name owes, at the bid on schedule and toward the ask as it falls
  behind. At most five orders per thirty seconds across the whole system,
  most-behind name first. Nothing is ever modified: an order resting unfilled
  past its TTL is cancelled and its shares rescheduled. From 15:00 the close
  cancels what rests and buys each remainder at market, paced to 15:25
- **slice** — the share quantity a single order is placed for: what the
  name's schedule owes, never less than the floor (configurable, default 1)
- **rebalance sell** — the deliberate way shares leave the portfolio: a CSV
  upload of symbol and quantity, executed after the day's plan, sold on the
  buy's rounds and closed at market; when the batch closes its net proceeds
  become a `reinvest` allocation to the engine (unless uploaded with reinvest
  off), shown as the day's sale proceeds and spent the next day. Never a name
  the day is buying. A sale made directly
  at the broker is the other way out, and is recorded rather than prevented
- **postback** — Zerodha's webhook carrying order state changes
- **XIRR** — the portfolio's return metric; daily irregular contributions
  make simple percentage return meaningless

## Hooks

`core.hooksPath` is `.githooks/`, so hooks are versioned and shared.

- `commit-msg` — Conventional Commits validation
- `pre-commit` — credential scan, then ruff check and format on staged Python

Escape hatches exist (`SKIP_SECRET_SCAN=1`, `SKIP_LINT=1`, `--no-verify`) but
the credential scan protects a live broker key and a TOTP seed. A leaked
credential is not fixed by deleting the commit — it is fixed by rotating the
credential.
