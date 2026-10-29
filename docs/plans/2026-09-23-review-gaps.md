# Deep review — correctness gaps

A full read of the engine, orders, gains, portfolio and records against the
PRD, 23 Sep 2026. Baseline before any of this: 1192 passed, 5 skipped, ruff
clean. Every item below was proved with a failing test before it was written
down; none is inferred from reading alone.

Status: `[ ]` open, `[x]` done and tested.

## P0 — the records are wrong

- [x] **A sell fill never consumes lots** (M36). `paperfill._fill` calls
  `lots.from_fill`, which returns `None` for a sell and stops there. Nothing
  calls `fifo.consume`. A rebalance sell — the *only* deliberate way shares
  leave the portfolio (M32, M34) — fills, completes, and leaves the lot
  history saying the shares are still held. No realised-gain row is ever
  written for a sale this system makes, so M37's report is empty by
  construction and the next sell can over-sell against phantom lots.
  *Proved: sell 4 of 10, lot remaining 10 → 10, realised rows 0.*

- [x] **A live buy fill creates no lot** (M35). Lots are created only on the
  paper path. `reconcile.record_fills` is how a live day's fills arrive, and
  it writes the `Fill` and nothing else. The first live day would buy shares
  with no cost basis, and FIFO would have nothing to consume.
  *Proved: 1 fill recorded, 0 lots created.*

- [x] **The 15:00 fallback ignores the day's state.** `fallback.run` never
  looks at `tradesheet.state`. A day the operator skipped (M15a) still has
  its lines, so the fallback cancels nothing, buys the full quantity at
  market, and `_settle_state` overwrites SKIPPED with UNFILLED. The skip is
  silently reversed by a timer.
  *Proved: SKIPPED day → 1 fallback order for 3 shares, state now UNFILLED.*

## P1 — invariants that do not hold

- [x] **`rank()` drops the held value of every excluded name.** The comment
  above the sum says the opposite of what the code does: a name held but
  above the price ceiling, suspended, or being sold is `continue`d before it
  reaches `usable`, so its value is missing from `portfolio_value`. Every
  current weight and every gap is then measured against a portfolio smaller
  than the real one (PRD §6.2).
  *Proved: hold 10 × ₹1,000 and 10 × ₹5,000 with a ₹2,000 ceiling →
  portfolio_value 10,000, not 60,000.*

- [x] **`Correction` is not append-only**, though PRD §7 marks it so. Every
  other immutable table has a trigger; this one has none and takes an UPDATE.
  *Proved: `.update(reason=...)` succeeded.*

- [x] **HALTED is never set.** §11 and M51 require three consecutive
  rejections to halt the day rather than re-alert every five minutes. The
  state exists in the enum, the settings page lists it, the deck colours
  it — and no code path reaches it.

- [x] **M18b / M31c are absent.** `SessionDay` has no settlement flag, so the
  settlement calendar the PRD calls for does not exist and the due-date rule
  cannot. `divergence.scan()` therefore compares lots against holdings synced
  that *morning*, before the day's fills — the recurring false alarm M31c
  exists to prevent.

## P2 — durability and accuracy

- [x] **The order row is not committed before the broker call.**
  `placement.place`'s docstring calls this the worst failure available and the
  reason the tag exists; `_reserve`'s `atomic` is a savepoint inside
  `run_tick`'s transaction, so it is not a commit. A crash between the
  broker's acceptance and the outer commit leaves an order at the exchange
  with no local row — and `reconcile.from_broker` only walks *local* orders,
  so nothing would ever find it.

- [x] **The DP charge is applied per fill, not per scrip per day.** A sell
  ladders into many fills by design, and each one is charged ₹15.34.

- [x] **`fifo.consume` does not assert it consumed the whole quantity.** If
  lots run out mid-loop it under-realises silently.

- [x] **Production settings**: `DEBUG` is not forced off, sessions have no
  age though §3 calls them short-lived, and there is no
  `CSRF_TRUSTED_ORIGINS` for the nginx deployment.

- [x] **The XIRR property test was checking the wrong thing.** Hypothesis
  found a near-total loss where it failed and the answer was right: the test
  discounted from a date before the first flow, where at a rate near -100%
  the terms reach 1e20 and cancel. Asserted in rate space now -- the Newton
  correction, which is the distance to the true root -- and the solver's
  worst case over 18,000 random flow sets is 3e-15. No production change.

## P3 — Phase 4

- [x] **Terraform** (M46–M50) in `infra/`: VPC, instance, elastic IP, IAM
  instance role, Secrets Manager entries left empty on purpose, S3 backup
  bucket with lifecycle, DLM snapshots, CloudWatch log group, metric filters
  and alarms, SNS, SSM-only access. With the systemd units and nginx config
  the schedule needs. Authored and never applied; Terraform is not installed
  here, so it has had no `fmt`, `validate` or `plan`.
