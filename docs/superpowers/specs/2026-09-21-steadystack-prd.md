# steadystack — Product Requirements Document

**Date:** 2026-09-21
**Status:** Draft for review
**Prioritisation:** MoSCoW

---

## 1. Purpose

A long-horizon equity accumulation system. It invests a fixed rupee amount
into Indian listed stocks every trading day, through the Zerodha Kite Connect
API, against a configured set of target weights — self-correcting toward
those weights by always buying whatever is most underweight.

The portfolio is earmarked for a child, on a horizon
measured in decades. The design consequence of that horizon is not
sophistication but durability: the system must be cheap to run, boring to
operate, and completely auditable years after any given trade.

## 2. Context and constraints

These are stated up front because each one would otherwise be discovered
expensively during implementation.

**The account is not the child's.** Zerodha issues demat accounts to minors
with a guardian, but does not grant Kite Connect API access on them. This
system runs against an adult's account with the holdings earmarked for the child.
Nothing in the schema or UI should imply otherwise. Transferring the corpus
to the child at majority is an off-system, manual act — likely an off-market
transfer with its own tax treatment, out of scope here.

**Kite Connect has a subscription cost, but a smaller one than first
assumed.** Order and account APIs are free. The market-data APIs this system
needs — the tick websocket and the depth calls the ladder prices from — are
₹500/month at time of writing. Against M8's ₹1,000/day default that is about
2.4% of annual contributions; at the ₹2,600/day currently configured it is
under 1%. It is no longer a material drag, but it should
still be visible in the performance dashboard rather than invisible. The
₹500/month figure was confirmed with Zerodha in September 2026; it should be
re-checked annually rather than assumed.

**SEBI regulates retail algorithmic trading, but not at this volume.** The
retail algo framework in force from 1 April 2026 requires exchange
registration and a unique exchange-assigned algo ID only for algos exceeding
ten orders per second. This system places at most five orders in any thirty
seconds, across every name and stream (M11c) — a few hundred a day at the
outside, never near one a second. No registration and no algo ID is
required, and none is stored in configuration. Zerodha's own ceilings (ten
orders a second, four hundred a minute, five thousand a day, per account)
are further away still.

**Two exchange rules shape every order.** Since 1 April 2026 a MARKET order
without a market-protection percentage is rejected, so the close's market
orders carry the sanity band as their protection (M22a). And since 6 April
2026 an order priced within 0.75% of the last traded price is exempt from
the order-to-trade-ratio penalty, so no limit is ever priced outside that
band (M20).

**A static IP is the launch gate.** Zerodha requires a static IP mapped to the
API key before it will accept orders placed through the API. The elastic IP
provisioned in Terraform must be registered with Zerodha before the first live
order, and the system must refuse to place orders until configuration records
that it has been. Confirm both of these with Zerodha directly — they are the
compliance surface, and they change.

**Order placement moves real money.** Every requirement below that touches
the ladder, the cash ledger, or the rebalance sell is a financial-correctness
requirement, not a feature.

## 3. Users and access

A single operator — the parent. No multi-user support, no roles, no sharing.
Authentication is username, password and TOTP. Sessions are short-lived.

## 4. Glossary

| Term | Meaning |
|---|---|
| **Tradesheet** | The day's buy decision: the full ranking snapshot, the chosen symbol, the quantity, and the cash it may spend. One per trading day. |
| **Gap** | A holding's current portfolio weight minus its target weight. The most negative gap is the most underweight. |
| **Carry** | Cash the day did not deploy, rolled into the next trading day — whether because the budget could not buy a whole share, because nothing filled, because the day was skipped, or because a lumpsum was credited. |
| **Ladder** | The placement loop. Every thirty seconds a *round* checks each name against its schedule — an even spread of its quantity across the placement window — and places a *new* limit order for what it owes, priced from live depth: at the bid while on schedule, stepping toward the ask as it falls behind. Orders are never modified; one resting unfilled too long is cancelled and its shares rescheduled. From 15:00 the close cancels what rests and buys each name's remainder at market, paced in rounds to 15:25. |
| **Slice** | The share quantity a single order is placed for: what the name's schedule owes, never less than a configurable floor, default 1. |
| **Lumpsum** | An ad-hoc contribution outside the daily amount — a bonus, a gift. Credited to carry and spent as prices allow, from the next tradesheet onward. |
| **External transaction** | A buy or sell made directly in the broker's own interface, outside this system. Detected by reconciliation, recorded by hand so that cost basis and FIFO stay correct. |
| **Divergence** | The system's holding for an instrument disagreeing with the broker's. Classified by the operator, never absorbed silently. |
| **Rebalance sell** | The deliberate mechanism by which shares leave the portfolio: a CSV upload of symbol and quantity. A sale made directly at the broker is the other way out, and is recorded rather than prevented (M34). |
| **Postback** | Zerodha's webhook carrying order state changes. |
| **Lot** | An individual purchase with its own acquisition date and cost basis, consumed FIFO on sale. |
| **Trace ID** | `ts-YYYY-MM-DD`, stamped on every log line, audit event and broker call for that trading day. |

## 5. Requirements — MoSCoW

### 5.1 Must have

**Authentication and access**
- M1. Username + password + TOTP login. TOTP enrolment via QR code.
- M2. All application views require an authenticated, OTP-verified session.
- M3. Rate limiting and lockout on repeated failed login attempts.

**Kite session lifecycle**
- M4. Home page displays current Kite session status and expiry.
- M5. One-click trigger of the Kite login flow; `request_token` exchanged for
  an `access_token`, stored encrypted at rest.
- M6. Session status is checked at 09:00; a missing session raises an alert.
- M7. Login completed after a tradesheet has parked resumes that tradesheet
  immediately, provided the time is before the market-fallback cutoff.

**Configuration**
- M8. Configurable daily investment amount (default ₹1,000).
- M8a. Ad-hoc lumpsum contributions. A lumpsum posts as a contribution to
  unallocated cash and, in the same transaction, an allocation to the
  engine's book, becoming available to the *next* tradesheet — never to one
  already generated, whose ranking snapshot and sizing are frozen. Money
  received on a day that has not yet placed an order
  is that day's: regenerating writes a new version that spends it. Once the
  day has placed its buy, it waits for the next trading day — a replan
  (M15i) spends only what the day already had, never money credited after
  its first order. Audited with actor, amount and reference.
- M9. Configurable instrument universe with target weights, validated to sum
  to 100%. Weights are versioned with an `effective_from` date — a historical
  tradesheet must be explicable against the weights that were live that day.
- M10. Configurable clock: tradesheet time (09:45), placing round interval
  (30 s), placement window close (14:00), market-fallback time (15:00) and
  the close's end (15:25).
- M11. Configurable maximum price per share, excluding unaffordable
  instruments from the universe.
- M11a. Placement is spread across the session rather than front-loaded.
  Each name the day buys has its own even schedule across the placement
  window: at any moment it should have committed `ceil(quantity × fraction
  of the window gone)`, reckoned one round ahead so the last share is due
  before the window shuts. A round places what the schedule owes, never less
  than the slice floor. The schedule starts at the window's open, or at the
  authorization if that came later (M15f) — a day authorized at 11:20
  spreads what it has over what is left rather than paying the ask to catch
  up. An outage is treated the same way: rounds that never ran (no claim —
  the program down, no Kite session, the system paused, or a round that
  failed) hold the schedule still, and what is left is spread evenly over
  what is left of the window. A round that ran and got no fill is not an
  outage; the name is behind and is chased toward the ask (D10). The window closes at a configurable time, default 14:00, an hour
  before the market fallback, so the last orders still have time to fill.
- M11b. Instrument tick size comes from the Kite instrument master, refreshed
  daily, and is never hardcoded. Order prices are rounded to the instrument's
  own tick size.
- M11c. The order rate is shared across the whole system: at most
  `max_orders_per_window` orders (default 5) in any `order_rate_window_seconds`
  (default 30). A round places only what that window has room for, counted by
  the same function as the rate breaker, most-behind name first (then most
  owed, then by name); the rest wait a round and, being further behind by
  then, go first next time. A stable order would starve the tail of a long
  list without saying so. The breaker stays as a backstop that trips only if
  something places without counting.
- M12. *Withdrawn.* The system holds no tax configuration at all — no rates,
  no exemption, no holding-period threshold. See M37 and §5.4.
- M12a. The static IP registered with Zerodha is recorded in configuration.
  The engine refuses to place any order while it is unset, and says so rather
  than failing at the broker. Changing it is audited like any other
  configuration change.

**Daily investment engine**
- M13. At the tradesheet time on each trading day, compute each instrument's
  current weight, its gap against target, and rank ascending by gap.
- M14. Available cash is the daily amount plus carry from prior days.
- M15. Walk the ranking from the most underweight name down and buy every
  name the day can: each takes `min(floor(remaining cash / price), shares to
  reach its target)` (M15c), and the walk continues with what is left until
  no remaining name can be bought. A day therefore buys as many names as its
  cash reaches, not one. The price is the best *ask* — not the last traded
  price — because an order the budget cannot reach can never fill. Last
  traded price remains the basis for weights, valuation and the gap ranking;
  the ask is used only to decide affordability and size. Affordability under
  this rule is also what M15d counts when a name is passed over.
- M15e. A name with no ask — the market is shut, or nobody is offering — is
  sized at its previous close rather than dropped from the day, and the
  tradesheet records which price each line was sized at. The ladder still
  places at the live book during market hours; the previous close only
  decides whether and how many.
- M15c. A single day's buy may not carry a name past its target weight,
  measured against the portfolio the day can reach (held value plus the
  day's cash). Quantity is `min(floor(cash / price), shares needed to reach
  target)`. If that yields zero — the name is underweight by less than one
  share — the walk moves on. Cash that no name can take carries forward.
- M15d. When the top-ranked name is unaffordable, the engine does not
  silently buy around it for ever. After a configurable number of
  consecutive trading days in which the same name has ranked first and been
  unaffordable — default 5 — the reserve engages: each engaged day sets aside
  one more daily amount toward that name (never more than its share price)
  and spends only the rest across the other names. Carry grows by the amount
  set aside until the name can be bought, which ends the wait. The reserve
  is visible on the tradesheet and raises a notice on the day it begins.
- M15a. The operator may override the day: change any one name's quantity —
  lower, down to what has already filled, or higher, while the day's cash
  still covers every line at the price it was sized at — skip one name for
  the day, exclude individual ticks from placing, or skip the day entirely. An override never rewrites the ranking snapshot — it is a
  separate, immutable decision recorded against it, with actor and
  before/after. A quantity reduction is accepted at any time before the day
  is fully filled, and takes effect through M15b. A skip is refused once
  anything has filled — there is no way to unwind a fill, and a day with
  fills is PARTIAL, not SKIPPED.
- M15f. A tradesheet is planned automatically but placed only once a person
  has authorized it. Until its latest version is authorized, the ladder places
  nothing for the daily buy — each tick records "not authorized" — and the
  15:00 fallback buys nothing at market; a day never authorized ends SKIPPED
  and its whole amount carries. Authorizing is per version and audited with
  actor and time: a regenerated version starts unauthorized, because what was
  approved was the earlier plan. Rebalance sells keep their own explicit
  execute step (M32) and are not affected.
- M15g. Today's tradesheet is generated or regenerated only while the market
  is in session. Before the open the book is the previous evening's
  leftovers — some names with an unmatched seller, most with none — and a plan
  built on it is chosen by accident; after the close nothing it plans can
  fill. The scheduled 09:45 generation is inside the session. A holiday still
  writes its closed-day record, other dates may be planned for replays and
  tests, and an explicit override exists for testing.
- M15h. Until the day has placed its first order it may be regenerated from
  the browser, with a reason, as a new version (D13); money credited that day
  before any order is out belongs to the day (M8a) and a regenerated version
  spends it. A trading day with no tradesheet yet may be generated from the
  browser by the same path the timer takes. Once the day has bought,
  regeneration is refused for good — a new version starting from nothing
  committed would buy the day again — and the way through is M15i.
- M15i. A day that has bought and been stopped (Stop today) may be replanned:
  a new version, with a reason, that ranks against everything the day has
  bought, spends only what the day has left — its opening less every
  version's spend and charges so far — and plans against the model portfolio
  as it is now. It leaves out any name with a sell order that day, carries the
  reserve over rather than recounting it, and starts unauthorized (M15f). It
  is refused with orders still out, before anything has been bought (that is
  a regeneration), within two minutes of the fallback, after settlement, while
  paused, and in live mode until it has been verified against a live day. The
  day still settles into one ledger row that sums every version's fills
  (M40a); the earlier version stays as it was stopped. An override is refused
  on any version that is no longer the day's latest.
- M15b. Reducing the day's quantity below what is already committed cancels
  the most recently placed open orders until committed fits the new
  quantity — the one case in which an order is cancelled before the fallback,
  and an explicit exception to M21. Orders that have already filled are never
  unwound. A tick exclusion does not reduce the day's quantity: the slice it
  would have placed stays unfilled and is bought by the 15:00 fallback unless
  the operator also reduces the quantity.
- M16. Persist the complete ranking snapshot on the tradesheet, so the
  selection is explicable without recomputation, forever.
- M17. Carry forward is computed from *actual* fill price and filled
  quantity, never from the planned price. An unfilled day carries the full
  amount; a partial fill carries the unspent remainder.
- M17a. A generated tradesheet places no order until the operator authorises
  it. It enters `AWAITING_APPROVAL` and waits. Authorisation is one audited
  act recording the actor, the time, and the exact ranking snapshot approved,
  so the record says what was agreed to and not merely that something was.
- M17b. Authorisation expires at the market-fallback cutoff. A day not
  authorised by then is `SKIPPED` with the full amount carried, and
  authorising after the cutoff is refused rather than honoured late.
- M17c. On authorisation the ladder starts at the tick the clock implies, not
  at tick 0, identically to a session resume and for the same reason (§6.4):
  tick numbering follows the clock, so a day authorised at 11:20 inherits the
  ladder position that time of day implies.
- M17d. The tradesheet may be regenerated before authorisation. Regeneration
  recomputes the ranking against current prices, supersedes the previous
  snapshot without destroying it, and clears any approval and any overrides —
  approving a sheet is approving *that* sheet, and a regenerated one has not
  been seen. A replanned version (M15i) starts unauthorized for the same
  reason.
- M17e. Consecutive unauthorised days raise an escalating alert. Carry
  accumulating quietly looks exactly like a system that is working, which is
  the failure this catches.
- M18. The system trades only on days the exchange holds an ordinary equity
  session. The calendar stores the session, not a list of holidays, because a
  deny-list would read "not a holiday, therefore normal" and ladder into a
  one-hour Muhurat evening all afternoon. Weekends never trade, whatever a row
  says: NSE occasionally runs a special Saturday session, and this system sits
  it out rather than trading a day nobody is watching. The exchange's
  published list is the source and nothing is guessed. It must be loadable a
  year ahead, and shrinking coverage raises an alert before it runs out,
  because an uncovered weekday reads as an ordinary session.
- M18a. A weekend date may be recorded only as closed. Recording one as a
  normal or special session is refused, with its reason, so the calendar can
  never assert a session the engine will silently ignore.
- M18b. Settlement is tracked separately from trading, on the same date. The
  two genuinely differ: the exchange publishes settlement holidays on which
  trading is ordinary and clearing is shut. Weekends neither trade nor
  settle. Both come from the exchange's published lists, loaded together.

**Order execution**
- M19. At each tick, while the day's quantity is not yet fully committed,
  place a *new* CNC limit order for one slice, priced from live five-level
  market depth. The last slice of the day is the remainder: where the
  uncommitted quantity is smaller than the slice size, the order is placed
  for the remainder, never rounded up.
- M19a. Orders are either **live** or **paper**. A live order goes to Kite. A
  paper order is recorded exactly as a live one would be — same price, same
  quantity, same tick, same depth snapshot, same tables, same state machine,
  same audit events, every row marked paper — but is never sent. There is no
  separate simulator and no separate dry-run mode: paper is the ordinary
  operating mode of any environment that is not placing real orders.
- M19b. The mode is configuration, default paper. Switching to live is an
  audited change and is refused unless the registered static IP is recorded
  and matches the address the system is running from (M12a). A development
  machine can therefore never place a live order by accident — not because
  the environment is special-cased, but because it fails that one condition.
  Attempting it is refused by the engine before any call is made, rather than
  being sent and rejected by the broker.
- M19c. A paper order fills against the live tick feed, not against a
  simulation. A paper buy is filled when the traded price reaches its limit
  price or better; a paper sell mirrors it. Market data is real in every
  environment, because M12a's static IP restricts order placement and not
  reads. This is what makes fills, lot creation, FIFO consumption and carry
  settlement exercisable before the first live order without any simulated
  broker — the market does the simulating.
- M20. A name's order is priced by how far behind its schedule its *fills*
  are, measured in time: `bid + urgency × (ask − bid)`, rounded down to the
  instrument's tick, where `urgency = min(1, seconds behind / (catch_up_minutes
  × 60))`, default 30 minutes. On schedule it rests at the bid; half an hour
  behind it pays the ask. Measuring in time rather than in shares makes the
  rule mean the same at any quantity and any spread. The price is then held
  within 0.75% of the last traded price — the exchange's order-to-trade
  exemption band — and never above the ask. At the sizes this system trades
  there is no market impact to manage; the spread is the only saving a
  passive order can make, so a name that is keeping up asks for all of it.
- M20a. If the book is locked or absent — bid equal to ask, one side empty,
  or the instrument frozen in a circuit — the name places nothing that
  round, the condition is recorded with the depth read, and the day
  continues. The market fallback still runs at 15:00.
- M21. Orders are never modified. Each order carries its own broker order ID
  for the life of the day and means what it meant when placed. A name has at
  most one limit order resting; one unfilled after `order_ttl_minutes`
  (default 5) is cancelled — not repriced — and its shares go back into the
  schedule, to be placed again the following round at what the book says
  then. The round that cancels does not replace in the same breath: a cancel
  can race a fill, and the next round sizes from what has been confirmed.
  The last round before the window closes cancels nothing, since its
  replacement would have no round to go out in.
- M21a. Committed quantity is filled plus open. No order is placed once
  committed equals the name's quantity. Every change in a name's answer —
  placed, resting, on schedule, waiting its turn, locked book — writes a tick
  row with the depth it read; a round that finds every name as before writes
  nothing new, and the row in force answers "why did nothing happen at
  11:05?".
- M22. From the fallback time the close runs in rounds, one every thirty
  seconds, to the close's end (default 15:25). The first cancels every limit
  order still resting, waits for every cancellation to be confirmed and
  re-reads fills; sizing from the pre-cancel snapshot would over-buy whenever
  an order fills in the moments before its cancel lands. Each round then
  buys at market `ceil(remainder / rounds left)` of each name, within M11c's
  rate, most still-to-buy first. Spreading across the close tracks the
  closing price, which for most names is the volume-weighted price of the
  last half hour; a single print at 15:00 is exposed to whatever that minute
  does. A close run by hand before its window is the only round there will
  be, and buys everything left.
- M22a. Each market order is bounded twice. Before placement, by a sanity
  band: if the last traded price is further from the name's reference price
  than a configurable percentage — default 5% — that name is withheld, said
  once rather than every round. At the exchange, by market protection set to
  the same percentage, without which a market order is rejected. A withheld
  day ends PARTIAL or UNFILLED, the unspent amount carries in full, and an
  alert is raised. The market order is the only unbounded price in the
  system; this stops a bad tick or a circuit move spending the day's cash at
  an absurd level, at the cost of an occasional unfilled day.
- M23. A fill reduces the unfilled quantity. Placement, the fallback and the
  carry calculation all operate on what remains, never on the original
  quantity.
- M24. Every scheduled task is idempotent. A tick that fires twice must not
  place two orders. Enforced by `flock` on the systemd unit *and* a unique
  constraint on `(trading_date, task, stream, tick_no)`, where `stream`
  distinguishes the daily buy from each rebalance batch — without it a sell
  batch running alongside the daily ladder collides on the same key.

**Real-time order and price feed**
- M25. A postback endpoint receives Kite order updates. Authenticity is
  verified against the Kite checksum; unverified posts are rejected and
  logged.
- M26. Postback handling is idempotent — the same update may arrive more than
  once, and updates may arrive out of order.
- M27. A fill arriving by postback immediately reduces that order's unfilled
  quantity, and with it the day's committed and unfilled totals, so the next
  tick sizes its slice against the true remainder rather than a stale one.
- M28. The five-minute poll remains as a reconciliation backstop for
  postbacks that never arrive. Postbacks are an optimisation, never the sole
  source of truth.
- M29. A websocket tick daemon subscribes to the instrument universe and
  maintains current prices, under its own supervised service.
- M30. The tick daemon reconnects with exponential backoff, and re-subscribes
  on a new Kite session. The dashboard displays a staleness indicator when
  the last tick is older than a configured threshold.

**Portfolio and rebalance**
- M31. Holdings are reconciled against Kite holdings daily; divergence raises
  an alert rather than being silently overwritten.
- M31a. A divergence is classified by the operator, not inferred by the
  system: an external buy, an external sell, a transfer, or a system error.
  Recording an external buy requires its acquisition date, quantity, price
  and charges, so lots are created with the correct cost basis and the
  correct acquisition date. Recording an external sell consumes lots FIFO and
  writes realised-gain rows exactly as a rebalance sell does. No divergence is
  ever silently absorbed into the holding.
- M31b. While an instrument carries an unresolved divergence, realised and
  unrealised gain figures *for that instrument* are withheld rather than
  computed from a holding the system knows to be wrong. Portfolio-level
  figures — XIRR, total value, absolute return — are still computed, but are
  shown flagged, naming the instrument and the quantity in doubt.
- M31c. Reconciliation compares holdings only once the shares are *due*. A
  CNC buy credits after settlement, so a settlement holiday (M18b) delays the
  credit by a day and the broker legitimately shows fewer shares than the
  ledger. That is not a divergence and must not be reported as one: a
  recurring false alarm is worse than no alarm, because it teaches the
  operator to wave the real one through. The due date is computed by counting
  settling days forward from the trade date, not calendar days.
- M32. Rebalance sells are uploaded as CSV (symbol, quantity), validated
  against available quantity, and previewed before execution. Execution is a
  separate, explicit confirmation — uploading a file never places orders.
- M32a. A running rebalance batch can be cancelled. Cancellation stops
  further placement and cancels open orders; fills already taken stand, and
  the batch closes partially executed with an audited reason.
- M33. Sell orders run the ladder in mirror, on the buy's rounds: each name
  runs to an even schedule from the moment the batch was executed to the
  placement close, one resting order at a time, priced at the ask and giving
  ground toward the bid as it falls behind (never under the bid, inside the
  OTR band), never modified; a sell resting past `order_ttl_minutes` is
  cancelled and rescheduled. A batch's round runs before the buy's and takes
  at most one order, so a sale is never starved and the rate limit holds. From
  the fallback time resting sells are cancelled and each name's remainder goes
  out at market, once. A batch is a `stream` for M24's idempotency key, claimed
  under task names of its own. Executing is the batch's authorisation: it does
  not wait for the day's buy (M15f), and it is refused on a shut or settled
  day, in live mode (until live sale fills are recorded), after the close,
  before the day's plan exists, for a name the day is buying, or when shares
  fall short across running batches. Stop today stops the buy only; a batch
  stops by its own cancel (M32a) or the system-wide stop, both of which wait
  for a round in flight and close the batch from its fills. The day's settle
  sweeps anything still resting and closes every batch; a batch never
  outlives its day.
- M33a. Sale money is reinvested (D-S1): when a batch closes, its net sale
  proceeds are settled and allocated to the engine (a `reinvest` allocation,
  unless the batch was uploaded with reinvest off), so they join that day's
  closing carry and the next day's plan spends them. No plan buys a name
  with a sell resting or filled that day, and no sell places for a name the
  day's plan buys (D-S2): CNC nets a same-day buy against a same-day sell.
- M34. The engine never sells on its own initiative. Shares leave the
  portfolio only by a rebalance sell, or by a sale the operator makes
  directly in the broker's interface and records under M31a. The second path
  exists because this is a live demat account the operator can trade in; it
  is recorded, not prevented.

**Lots and realised gains**
- M35. Every buy fill creates an immutable lot with its own acquisition date
  and cost basis. Cost basis is what the shares actually cost: `price × qty`
  plus every charge on the trade. No charge is carved out — the distinctions
  that matter for tax are the broker's report to make, not this system's.
- M35a. Opening positions are imported as immutable lots carrying their
  true acquisition dates, quantities and costs, one lot per original
  purchase. Import is a one-time, audited action. Until it is marked
  complete, the system refuses to compute realised gains or to execute a
  rebalance sell, because FIFO against an incomplete lot set is wrong in a
  way nobody would notice.
- M35b. Charges are estimated from a rate card at fill time so that carry can
  be settled the same evening, and the lot is marked `basis_estimated`. When
  the statement's settlement line disagrees, or a contract note for that day
  is available, the difference is posted as a new journal entry and the lot
  is superseded at its final basis (`basis_final`) — never by editing the
  estimate. Realised gains are never computed from an estimated basis.
- M36. Sells consume lots strictly FIFO — matching the convention the broker
  uses, so the two records can be reconciled — as Indian equity taxation requires,
  and write immutable realised-gain rows. A consumed lot is never mutated.
- M36a. An external buy may not be recorded with an acquisition date earlier
  than the last sell already matched for that instrument. Doing so would
  invalidate realised-gain rows that M36 makes immutable. Such a case is
  handled by a correction (M36c), not by insertion.
- M36b. A corporate action that changes existing lots — a split, a
  consolidation — supersedes rather than edits them. The original lots are
  marked superseded and remain readable; new lots are written carrying the
  original acquisition dates forward with the adjusted quantity and
  per-share basis, linked to the action that caused them. A bonus issue
  needs none of this: it is new lots at zero cost, inheriting the holding
  period of the lots that earned them.
- M36c. A correction is the only way to undo a bookkeeping error. It is an
  appended, audited entry that supersedes the rows it corrects and states a
  reason; it never edits a lot, a realised-gain row or a ledger row. Where a
  correction invalidates realised gains already reported, it writes reversing
  rows rather than deleting the originals.
- M36d. A transfer is modelled as an external transaction with side
  `TRANSFER_IN` or `TRANSFER_OUT`. A transfer in carries the acquisition date
  and cost basis it had with the transferor, so the acquisition date is
  continuous. A transfer out is a disposal at nil proceeds that consumes lots
  FIFO and is flagged for manual tax treatment — its treatment under Indian
  law depends on the relationship and the instrument, and the system records
  it rather than deciding it.
- M37. Realised gains are reported as economics, not tax: per sale and per
  lot consumed — proceeds, cost basis, gain — with a running total by date
  range. The system applies no classification, no rates, no exemption and no
  financial-year framing. The broker's tax P&L does all of that; this report
  exists so those numbers can be checked against records the system can
  prove.
- M38. Unrealised gains are reported per lot: acquisition date, cost basis,
  current value, gain.

**Dashboards**
- M39. Today's orders: quantity, filled, pending, limit price, current price.
- M40. Holdings: quantity, VWAP (average cost), current price, unrealised
  P&L, current weight, target weight, gap.
- M40a. Cash ledger: a daily summary per book — every trading day since
  inception with opening carry, daily allocation, extra allocation, sale
  proceeds, spend, charges, other and closing carry — plus a reconciliation
  proving it agrees with the journal over any range. Sale proceeds are the
  day's reinvested rebalance sale money net of the sells' own charges (M33a);
  they are neither a contribution nor part of what the day had before it
  spent. The books themselves are the journal, the general ledger per account
  and the trial balance (M40c).
- M40b. Share ledger: every quantity in and out, by instrument and by source
  — engine buy, rebalance sell, external transaction, corporate action —
  reconciling the system's holding against the broker's.
- M40c. The books: the journal, the general ledger per account and the trial
  balance, any date range, either book, exportable as CSV. A Console
  funds-statement upload is matched against them automatically where it can
  be, and queued for a person where it cannot; a settlement line that
  disagrees is corrected by a new entry that also finalises the lots it
  touches, within what rounding can explain. A line the app already
  recorded can be matched to its entry by hand, and each upload is
  summarised against the books for the chartered accountant. Zerodha's
  opening balance is checked against the books each trading morning. A
  financial year, once the chartered accountant has signed off and its
  statement ties out, is closed to any entry dated inside it. See
  `docs/superpowers/specs/2026-09-24-double-entry-journal-design.md`.
- M41. Portfolio performance: XIRR as the headline metric — daily irregular
  contributions make a simple percentage return meaningless — plus total
  invested, current value, absolute and percentage return, and per-instrument
  contribution. Shares held at go-live are an outflow on the day each lot was
  bought, at the cost of what is still held, and "contributed" includes the
  opening balance.
- M41a. Performance is reported against a benchmark index XIRR computed over
  the same contribution dates, so the return is judged against holding the
  index rather than against zero. The index is an instrument on the existing
  tick feed — its instrument token is configuration, default NIFTY 50 — so
  this adds no second market-data dependency.

**Traceability**
- M42. Structured JSON logs to stdout, captured by journald and shipped to
  CloudWatch Logs. Logs must survive loss of the instance; an EBS snapshot
  will not contain the last hour.
- M43. Every Kite request and response is persisted with latency and status,
  credentials redacted.
- M44. Every state transition and every configuration change writes an audit
  event with actor, action, and before/after values.
- M45. A single trace ID per trading day, stamped across logs, audit events
  and broker calls, so one query reconstructs a day end to end. Events that
  are not part of a trading day — a lumpsum recorded on a Sunday, a
  configuration change, an external transaction entered a week late — carry
  the trace ID of their own calendar date in the same form, so M44's "every
  audit event" holds without pretending they belong to a trading session.

**Infrastructure**
- M46. Single EC2 instance: nginx, gunicorn, Django, local PostgreSQL.
- M47. All infrastructure in Terraform. No manual console changes.
- M48. Secrets in AWS Secrets Manager, never in the repository, never in
  environment files on disk.
- M49. Nightly `pg_dump` to S3 with lifecycle-managed retention, plus
  scheduled EBS snapshots. Restore must be tested, not assumed.
- M50. HTTPS only, with HSTS. Shell access via SSM Session Manager rather
  than an open SSH port.

**Alerting**
- M51. Alerts on: missing Kite session at the 09:00 check, order rejection,
  a day halted after repeated rejection, a day ending unfilled, tick daemon disconnection, holdings reconciliation
  divergence, and any unhandled exception in a scheduled task.

### 5.2 Should have

- S1. *Withdrawn, replaced by M19a–M19c.* Paper mode is not an optional
  safety net bolted onto a live system; it is the default mode, and live is
  the exception that has to earn its way past M19b.
- S2. Health endpoint reporting session validity, tick freshness, last
  successful tradesheet, and database reachability.
- S3. Trading-day detail view: the full ranking, every tick with its depth
  snapshot — including the ticks that placed nothing and why — every order,
  and every fill, for any past date.
- S4. Kite Connect subscription cost recorded and surfaced as a drag on
  returns — as a share of contributions, as a share of gains, and as XIRR net
  of it.
- S5. Charge tracking per order, component by component, so cost basis is
  exact rather than estimated and the components are recoverable if they are
  ever needed elsewhere.
- S6. *Withdrawn.* Judging when a sale should wait is a tax question, and the
  system does not answer those.
- S7. Configuration change history browsable in the UI.
- S8. Corporate action handling — detection and alerting on splits and
  bonuses, which silently corrupt weights and cost basis. The adjustment
  itself is manual in v1: the operator records the resulting quantity and
  cost-basis change and it is audited like any other entry. Bonus shares
  enter at zero cost and inherit the acquisition date of the lots that earned
  them.

### 5.3 Could have

- C1. Charts: portfolio value over time, weight drift, contribution vs growth.
- C2. CSV and Excel export of holdings, realised gains and the audit trail.
- C3. Weight backtesting against historical prices.
- C4. Per-instrument notes and an investment thesis field.
- C5. Mobile-optimised dashboard.
- C6. Configurable split of the daily amount across several underweight
  names rather than a single instrument.

### 5.4 Won't have (v1)

- Mutual funds bought as mutual funds — no folios, no NAV-priced orders, no
  Coin, no fractional units. The exclusion is about the *plumbing*: none of
  it has an order book, depth or a tick feed, so none of it reaches the
  ladder. A **listed ETF is permitted** where no single stock can express the
  exposure, because it is an ordinary cash-segment instrument the engine
  already knows how to buy — see the long-hold bucket design, `L2`. Gold is
  the one such case today; index ETFs remain excluded, since an index can be
  expressed in stocks.
- Derivatives, intraday, or any product other than CNC delivery.
- Multi-user, multi-portfolio, or sharing.
- Automated rebalance-sell suggestions. Sells stay a deliberate human act.
- Tax of any kind: no rates, no exemptions, no short- or long-term
  classification, no financial-year reporting, no ITR output. The system
  records what was bought, what it cost, what was sold and what that
  realised. Everything downstream of that is the broker's report and an
  accountant's job.
- Broker-agnostic abstraction. This is built for Kite.

---

## 6. Behaviour specifications

### 6.1 The daily cycle

```
09:00  session check ......... no valid Kite session -> alert
09:45  tradesheet ........... rank gaps, select instrument, size the order
                              no session -> park as AWAITING_SESSION
09:45  round 0 .............. each name places what its schedule owes:
                              LIMIT, CNC, validity DAY, at the bid
09:45:30  round 1 ........... names behind schedule place, nearer the ask;
 ...                          every 30 s, at most 5 orders per 30 s in all,
                              most-behind first; stale orders cancelled
14:00  window closes ........ what rests has an hour to fill
15:00  close, round 0 ....... cancel what rests, then MARKET for a share of
 ...                          each remainder, every 30 s, to 15:25
15:45  end of day ........... reconcile, settle carry, close the ledger row
```

The tradesheet and round 0 share the 09:45 slot but not the moment: round 0
runs only once the tradesheet has committed and been authorized, and does
nothing if it has not. Round numbering is by the clock, not by execution —
the round at 10:15 is round 60 whether or not rounds 1 to 59 placed
anything, so a day that resumes after an outage places where the day has
got to.

### 6.2 Selection

Available cash is the configured daily amount plus accumulated carry.
Portfolio value is the sum of holdings valued at last traded price. Each
instrument's gap is its current weight minus its target weight; the ranking
is ascending, so the most negative gap sorts first.

The engine walks that ranking and takes the first instrument whose best ask
is at or below available cash. Quantity is the smaller of what the cash buys and what
the target allows: `min(floor(cash / price), shares to reach target)`. A name
underweight by less than one share yields zero and the walk moves on.

*Worked example — cash ₹1,700 (₹1,000 daily + ₹700 carry):*

```
rank  symbol     gap      ltp        affordable   decision
  1   MRF       -4.1%   142,000.00       no       skip
  2   HDFCBANK  -2.8%     1,650.00      yes       BUY 1  -> carry 50.00
  3   ITC       -1.9%       420.00      yes       —
```

On day one the portfolio is empty, every weight is zero, and every gap equals
the negative of its target — so the ranking resolves naturally to the highest
target weight. No special-casing is required.

Two cases sit either side of that walk, and both are handled deliberately.

**A large cash balance does not concentrate.** After a lumpsum, or a run of
unfilled days, a greedy `floor(cash / price)` would put everything into the
most underweight name and carry it well past target — ₹52,845 buys thirteen
shares of a ₹3,912 name and moves it from 2.56 points under to 2.09 points
over in a single day. M15c's cap stops at the target instead, and the
remaining cash carries into tomorrow, where the ranking has moved on.

**A name too expensive for one day is not skipped for ever.** Without a rule,
a name priced above available cash ranks first, is passed over as
unaffordable, and ranks first again tomorrow — indefinitely, because a
cheaper name in the universe keeps consuming the carry that would have
reached it. M15d stops buying after five such days and lets carry build until
the name is affordable. The portfolio spends a few deliberate days idle in
exchange for the ranking meaning what it says.

### 6.3 Carry

Carry is settled from what actually happened, not what was planned:

| Outcome | Carry forward |
|---|---|
| Fully filled | `cash − (filled_qty × avg_fill_price) − charges` |
| Partially filled | `cash − (filled_qty × avg_fill_price) − charges` |
| Unfilled | the entire `cash` |
| Skipped before any fill (holiday, no session by cutoff, or operator) | `cash` |
| Skipped by the operator after a fill | treated as partially filled — carry is what was genuinely not spent, never the whole `cash` |
| Lumpsum recorded | a contribution to unallocated and an allocation to the engine, spendable from the next tradesheet |

Charges are part of the subtraction because they are money that left the
account. Omitting them makes the ledger's reconciliation identity (M40a) fail
by the charge amount every trading day, and compound.

Spend can exceed what the quantity was sized against in two ways: the 15:00
market order fills above the last traded price, or the later slices — priced
up the ladder toward the ask — fill higher than the price M15 sized against.
Carry may therefore be smaller than planned, and may be zero. It must never
be allowed to go negative: whenever total spend plus charges exceeds
available cash, the excess is recorded as a negative adjustment against the
*next* day's budget, and the event is logged at warning level. This applies
to limit fills as well as to the market fallback.

### 6.4 Session lifecycle

Kite access tokens expire daily. The operator initiates login from the home
page; the app redirects to Kite, receives a `request_token` on return, and
exchanges it with the API secret for an `access_token`, which is stored
encrypted.

A tradesheet generated without a valid session parks in `AWAITING_SESSION`.
On successful login before the fallback time, the ladder starts immediately
from that point and still receives its market fallback at 15:00. Because tick
numbering follows the clock (§6.1), a day that resumes at 11:20 begins at
tick 19, not tick 0 — it inherits the ladder position the time of day implies
and does not restart at the bid. A login after the fallback time leaves the
day skipped, with the full amount carried.

### 6.5 Order state machine

```
Because orders are never modified, there are two machines: one for the day,
one for each order the day places.

Tradesheet — one per trading day:

DRAFT
  └─> AWAITING_SESSION ──(login before cutoff)──┐
  └─> AWAITING_APPROVAL <────────────────────────┘
        │     └─(regenerate)─> AWAITING_APPROVAL, new snapshot
        └─(operator authorises before cutoff)─┐
  └─> RUNNING <───────────────────────────────┘
        ├─> FILLED     day quantity fully filled              (terminal)
        ├─> PARTIAL    some filled at close, or skipped by
        │              the operator after a fill              (terminal)
        ├─> UNFILLED   nothing filled at close                (terminal)
        ├─> HALTED     stopped early on repeated rejection    (terminal)
        └─> SKIPPED    holiday, no session, not authorised by
                       the cutoff, or operator before any
                       fill                                   (terminal)

Order — one per placing tick, never modified:

PLACED ──(broker accepts)──> OPEN
   └───(broker refuses)────> REJECTED   with the reason   (terminal)

OPEN
  ├─> PARTIALLY_FILLED ──> COMPLETE                        (terminal)
  │                   └──> CANCELLED  at 15:00, remainder  (terminal)
  ├─> COMPLETE                                             (terminal)
  └─> CANCELLED        at 15:00, or superseded by an
                       operator quantity reduction         (terminal)

The 15:00 market order is an order like any other and moves through the same
states; it is distinguished by its type, not by a state of its own. A market
order that the broker refuses lands in REJECTED and leaves the day PARTIAL or
UNFILLED, not FAILED.
```

Every transition writes an audit event carrying the trace ID.

### 6.6 Lots and realised gains

Each buy fill creates a lot. Cost basis is `(price × qty) + charges` — every
charge on the trade, with nothing carved out.

A rebalance sell consumes lots in strict FIFO order of acquisition, writing
one immutable realised-gain row per lot consumed:

```
sell 5 INFY @ 1,900 on 2027-03-10

  lot A  2026-01-15   2 @ 1,500   cost 3,000.00   gain  800.00
  lot B  2026-02-02   2 @ 1,550   cost 3,100.00   gain  700.00
  lot C  2026-09-20   1 @ 1,700   cost 1,700.00   gain  200.00
```

The report stops at the gain. No term, no financial year, no rates, no
liability. That deliberately leaves every tax question with the broker's own
report and with an accountant, and keeps this system to the one thing it can
prove: which lot was consumed, when it was acquired, what it cost, and what
it realised. FIFO is kept not because tax requires it but because the broker
uses it, and two records that match are worth more than two that don't.

Buying daily accumulates roughly 250 lots per instrument per year. Lot
matching must be indexed and paginated accordingly; this is not a small
table after a few years.

## 7. Data model

Core entities. Immutability is noted where it is an invariant, not a
preference.

| Entity | Purpose | Notes |
|---|---|---|
| `Instrument` | Tradeable universe | symbol, exchange, token, tick size, active |
| `TargetWeight` | Allocation target | versioned by `effective_from` |
| `TradeSheet` | One per trading day | state, cash, carry in/out, ranking snapshot (JSON) |
| `Order` | Buy or sell | one per placing tick, never modified; mode (live / paper), Kite order id where live, type, limit price, state, filled qty, avg price |
| `Tick` | One five-minute tick | tick no, bid/ask snapshot, the order placed or the reason none was, broker response |
| `Fill` | An execution | qty, price, timestamp, Kite trade id |
| `Lot` | One purchase | **immutable**; acquisition date, cost basis, remaining qty, basis state (estimated / final), superseded-by |
| `RealisedGain` | A lot consumed by a sell | **immutable**; proceeds, cost basis, gain |
| `Holding` | Current position | derived and reconciled against Kite |
| `JournalEntry` | One balanced movement of money | **append-only**; book (live / paper), gapless number per book, kind, occurred-on date; lines sum to zero; a mistake is undone by a reversal entry, never an edit |
| `JournalLine` | One account movement within an entry | signed amount, debit positive; account, optional instrument and charge component |
| `CashLedger` | Daily engine-cash summary, per book | **append-only**; opening carry, daily allocation, extra allocation, spend, charges, closing carry — a rollup of the journal's engine accounts, checked against it, never the source of truth |
| `StatementLine` | One row of an uploaded Zerodha funds statement | matched to a journal entry once (null → value never changed again), or queued for a person to classify |
| `BalanceCheck` | Zerodha's morning opening balance against the books | **append-only**; one row per day; a difference beyond a rupee alarms |
| `RebalanceSell` | Upload batch and rows | symbol, qty, validation state, batch state incl. cancelled |
| `TradesheetOverride` | An operator decision on a day | **immutable**; kind (quantity, skip, tick exclusion), before/after, actor, reason |
| `ExternalTransaction` | A trade or transfer made outside the system | symbol, side (BUY / SELL / TRANSFER_IN / TRANSFER_OUT), qty, price, charges, acquisition date, recorded-at, actor |
| `Correction` | Undoing a bookkeeping error | **append-only**; target rows, reversing entries, reason, actor |
| `OpeningImport` | The one-time takeover of an existing holding | per-lot rows, completed-at; blocks gains and sells until complete |
| `Divergence` | Holding disagreeing with the broker | symbol, expected, actual, detected date, classification, resolving entry |
| `KiteSession` | Broker session | encrypted access token, issued and expiry timestamps |
| `TaskRun` | Idempotency guard | unique on `(trading_date, task, tick_no)` |
| `BrokerCall` | Every Kite call | **immutable**; endpoint, request, response, latency, status |
| `AuditEvent` | Every transition | **immutable**; actor, action, entity, before/after, trace id |
| `SessionDay` | One dated exception to the ordinary week | date, kind (closed/normal/special), session hours for a special, whether it settles, note, loaded by |
| `PriceTick` | Latest price cache | written by the tick daemon, read by dashboards |

## 8. Traceability

Three layers, deliberately redundant, because each fails differently.

**Structured logs.** JSON to stdout, captured by journald, shipped to
CloudWatch Logs by the agent. Shipping is the point: an instance failure must
not take the last hour of logs with it. Every line carries the trace ID; HTTP
requests additionally carry a request ID.

**Broker call records.** Every Kite request and response persisted with
latency and status, credentials redacted. When the broker's record and yours
disagree, this table is the evidence.

**Audit events.** Actor, action, entity, before and after. Configuration
changes are audited alongside order transitions — a weight edit that caused
unexpected buying must be attributable to a person and a timestamp.

A day is reconstructed by querying one trace ID across all three.

## 9. Architecture

Single EC2 instance, chosen deliberately over a container or serverless
topology: one operator, one portfolio, roughly sixty scheduled ticks a day.

```
                    ┌─────────────────────────────┐
  operator ──HTTPS──│  nginx → gunicorn → Django  │
                    │                             │
  Kite postback ────│  PostgreSQL (local)         │
                    │                             │
                    │  systemd units:             │
                    │    steadystack-web.service      │
                    │    steadystack-ticker.service   │  ←── Kite websocket
                    │    tradesheet.timer  09:45  │
                    │    ladder.timer      */5    │  ───→ Kite REST
                    │    fallback.timer    15:00  │
                    │    eod.timer         15:45  │
                    └─────────────┬───────────────┘
                                  │
                 CloudWatch Logs  │  S3 (pg_dump)  │  Secrets Manager
```

Scheduled work runs as Django management commands under systemd timers, each
wrapped in `flock`. The tick daemon is a long-running supervised service with
automatic restart.

Terraform provisions the instance, security group, elastic IP, DNS, IAM
instance role, Secrets Manager entries, S3 backup bucket, CloudWatch log
groups and EBS snapshot lifecycle policy.

**The known trade-off:** a single instance is a single point of failure. If
it dies mid-ladder, every order the day has placed so far remains resting at
the broker with nothing watching them — up to one per elapsed tick, not one
in total. Mitigations are that orders are DAY validity and expire
at close, order state is recoverable from Kite on restart, and startup
reconciliation is therefore a required behaviour, not a nicety.

## 10. Security

- TOTP mandatory; no bypass path.
- Kite API key and secret, Django secret key and database password in
  Secrets Manager, fetched at boot via the instance role.
- Access token encrypted at rest; never logged, never rendered.
- Postback endpoint verifies the Kite checksum and is exempt from session
  auth but from nothing else.
- HTTPS only, HSTS enabled.
- No inbound SSH; shell access through SSM Session Manager.
- Credential scanning at commit time via the repository's pre-commit hook.

## 11. Failure modes

| Failure | Behaviour |
|---|---|
| No Kite session at 09:45 | Tradesheet parks; alert; resumes on login before cutoff |
| Login after fallback time | Day skipped, full carry, logged |
| Not authorised by the fallback time | Day skipped, full carry, logged; consecutive occurrences escalate (M17b, M17e) |
| Order rejected (margin, price band, circuit) | Alert with the broker's reason; the tick records it and places nothing further. Later ticks continue with their own slices, but three consecutive rejections halt the day (HALTED) rather than re-alerting every five minutes until close |
| Live mode requested without a matching registered static IP | Refused before any broker call; the system stays in paper and says why (M19b) |
| Last traded price outside the fallback sanity band | Market order withheld (M22a); day ends PARTIAL or UNFILLED, full unspent amount carries, alert raised |
| Market order partly filled at close | The unfilled remainder is abandoned; carry reflects only what was spent |
| Market order fill exceeds available cash | Negative adjustment to next day's budget; warning logged |
| Postback missed | Five-minute poll reconciles |
| Postback arrives out of order or duplicated | Deduplicated on order id, status and timestamp |
| Tick daemon disconnects | Exponential backoff reconnect; dashboard shows staleness; ladder unaffected, since it polls depth directly |
| Holdings diverge from Kite | Alert; no silent overwrite; gains for that instrument withheld until the operator classifies it (M31a, M31b) |
| Lumpsum recorded mid-day | Credited to the day's closing carry; the running tradesheet is untouched |
| Top-ranked name unaffordable for N days | Reserve engages (M15d): the day places nothing, carries in full, and raises a notice on the first such day |
| Book locked or one-sided at a tick | Tick records the condition and places nothing; the fallback still runs |
| Opening import incomplete | Realised-gain reporting and rebalance sells refused until it is marked complete |
| Contract note or settlement line disagrees with estimated charges | Difference posted as a new journal entry; lot basis moves to final |
| Corporate action (split, bonus) | Detected and alerted; manual adjustment in v1 |
| Instance failure mid-ladder | DAY orders expire at close; startup reconciles state from Kite |
| Timer double-fires | Rejected by `flock` and the `TaskRun` unique constraint |

## 12. Decisions and remaining unknowns

### 12.1 Settled

| # | Question | Decision |
|---|---|---|
| 1 | Algo registration | Not required below ten orders per second. The launch gate is a static IP registered with Zerodha (§2, M12a). |
| 2 | Tick size source | The Kite instrument master, refreshed daily (M11b). |
| 3 | Ladder steps | Priced by how far behind schedule a name's fills are, in time: the bid on schedule, the ask at `catch_up_minutes` behind, never outside the order-to-trade exemption band (M20). Replaced `ladder_patience`, which stepped on the clock whether or not orders were filling. A locked or one-sided book places nothing (M20a). |
| 4 | Universe size | An operator choice, made workable by the reserve. The practical constraint: a name priced above the daily amount × the reserve window is only ever bought out of a reserve, so size the universe knowing that. |
| 5 | First run | Existing holdings are imported as immutable lots with their true acquisition dates and costs (M35a). |
| 6 | Charge data | Estimated from a rate card at fill time, reconciled against the contract note, then the lot basis is final (M35b). |
| 7 | Concentration cap | A day's buy may not carry a name past its target weight (M15c). |
| 8 | Starved names | Reserve and accumulate after five unaffordable days (M15d). |
| 9 | Overrides after execution | Quantity reduction any time before fully filled; skip refused once anything has filled (M15a, M15b). |
| 10 | Slice size | Each name to its own even schedule across the window, placed in thirty-second rounds under one system-wide rate of five orders per thirty seconds (M11a, M11c); the close paced the same way to 15:25 (M22). |
| 11 | Benchmark source | An index instrument on the existing tick feed, no second dependency (M41a). |
| 12 | Back-dated external buys | Refused earlier than the last matched sell; handled as a correction (M36a, M36c). |
| 13 | Splits versus immutable lots | Superseding lots, originals retained and readable (M36b). |
| 14 | Transfers | `TRANSFER_IN` / `TRANSFER_OUT` on the external transaction (M36d). |
| 15 | System error | A correction entry that supersedes and states a reason (M36c). |

| 16 | Tax scope | Nothing tax-specific is built. FIFO lots and economic realised gains stay, because they are needed to know what was sold and to reconcile with the broker. Classification, holding-period thresholds, rates, exemptions and financial-year reporting are all out (M12 withdrawn, M35, M37, S6 withdrawn, §5.4, §6.6). |

| 19 | Approval gate | The day waits for an explicit authorisation before placing anything (M17a–M17e). Deliberately trades autonomy for a held breath: this buys shares for a child over eighteen years, and a wrong ranking caught by eye once is worth more than the mornings the tap costs. The cutoff makes inaction safe — an unauthorised day carries, it does not misfire. |
| 18 | Trading and settlement calendars | One dated record, two questions: does it trade, does it settle. Not a fourth session kind, because an ordinary trading day can be a settlement holiday. Weekends answer no to both and outrank any row (M18, M18a, M18b, M31c). |
| 17 | Broker simulation | None. The real Kite client is used everywhere; reads work in every environment and placement is gated by M12a. Untested execution paths are covered by paper mode filling against the live feed (M19a–M19c), not by a simulator. |

### 12.2 Still open

One thing remains, and it is not an engineering decision.

**Tax treatment of a transfer out.** M36d records one as a disposal at nil
proceeds and stops there. Whether it is taxable, and how the recipient's basis
is established, is for a chartered accountant — including the eventual
transfer of this whole corpus to the child at majority, which §2 puts out of scope.
With tax out of scope entirely this is no longer a question the system needs
answered; it is a question the operator does, before the first transfer.
Nothing in the build waits on it.

Two former unknowns are now closed. Kite pricing is confirmed at ₹500/month
for the market-data APIs (§2), and the static-IP registration path is known
to the operator (M12a). The daily amount is simply configuration: M8 ships a
₹1,000 default and the operator sets their own — ₹2,600 at time of writing —
with §2's cost reasoning and M15d's reserve window stated against both.

## 13. Success criteria

The system is working when:

- It places and fills the day's quantity, one slice per tick, once the day
  has been authorised, without further intervention.
- Carry reconciles exactly against contributions and fills, every day, with
  no drift.
- Any past trading day is fully explicable from stored data alone — why that
  instrument, at what prices, through which ticks, including the ticks that
  placed nothing.
- A rebalance sell produces a FIFO-correct realised-gain statement whose lots
  and cost bases reconcile line by line against the broker's own report.
- An ordinary day asks two things of the operator and no more: the morning
  login, and authorising the tradesheet. Both are doable from a phone in
  under a minute. Overrides, lumpsums, external-trade recording and
  corporate-action adjustments exist, but each is exceptional and audited.
- Holdings and the broker agree every evening, and any disagreement is
  classified by the operator rather than absorbed.
