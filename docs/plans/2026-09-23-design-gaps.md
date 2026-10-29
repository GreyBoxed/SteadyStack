# Design gaps — closing the screens

Audit of the fifteen artboards in `docs/design/` against the built templates,
23 Sep 2026. Tax and holding-period surfaces are withdrawn by the PRD and are
not listed. Where a board's in-page stepper became a server form with a
reason, the form stays — that is deliberate, and audited.

Charts are drawn server-side as inline SVG from coordinates computed in
Python. No JavaScript library, no SPA.

Status: `[ ]` open, `[x]` done and tested.

## P1 — the screens that do not exist, from data already stored

- [x] **Instrument day page** (`/orders/<date>/<symbol>/`) — Trading / ITC board
  - [x] KPI cards: filled/target, open orders, last traded, fill VWAP, deployed
  - [x] Lifespan chart: each order a bar at its limit from placement to fill/cancel/now, mid-price line from ticks, fill dots, 15:00 marker, NOW
  - [x] Tabs (server-side, `?tab=`): Orders (grouped open/complete), By tick, Fills (weighted average summary, lots created), Broker calls
  - [x] Depth ladder from `Tick.depth_snapshot`: 5 asks, spread, 5 bids, quantity bars, orders count, "yours" marker; pick any tick
  - [x] Slicing and ladder panel, day timeline
  - [x] Cash · today panel: daily, carry in, available, spent, open commitment, charges, projected carry
- [x] **Performance chart**: value vs invested vs invested + subscription from `DailyValuation`, 6M/1Y/All
- [x] Performance: contributed and days run; gain after subscription; contribution bars; median time to fill and price vs the ask; XIRR net of the subscription; did-it-run shows every day, one cell per day
- [x] **Main (Trading) board** on `web/orders.html`: KPI strip, per-instrument table with progress, VWAP, LTP, value, link to instrument page; blotter with placed time, side, broker id, fill time, value, stream; filter tabs

- [x] Pre-existing: a tradesheet generated after midnight IST stamps the wall-clock day's trace id instead of the trading date's (two failing tests)

- [x] **Tradesheet lock gap**: the lock releases once every order is terminal, so a regenerate after the day filled makes a new version the ladder would buy again. Refuse regeneration once any daily-buy order exists or the day has settled
- [x] **Regenerate from the browser**: button on the tradesheet page, shown only while the lock permits; reason required; audited; needs a Kite session for quotes

- [x] **`generate_tradesheet` ignored carry**: `--carry-in` defaulted to 0 and was always passed, so every planned day dropped its carry and lumpsums. Left out, it now reads the ledger
- [x] **Share ledger filter**: narrowed to one instrument, every other holding was reported as a divergence

## P2 — panels that change what the operator can see

- [x] Day detail: previous/next day, jump to today, days on record
- [x] Day detail: avg fill, carry out, ticks used cards; per-order broker id, placed time, depth at placement; lots created; charges breakdown
- [x] Tradesheet: lumpsum band; afford / max qty / decision columns and gap bar in the ranking
- [x] Today: tick N of M, open/value columns and cash/deployed/carry footer in "what is running", session hours left and re-login
- [x] Holdings: sort toggle, avg cost, unrealised, lots, under/over bar; 90-day drift chart
- [x] Awaiting session: timeline bar, "what is being held", "how today got here" trail, trace id and state header
- [x] Ledger: CSV export (cash and shares); instrument filter control; extra cash columns (symbol, qty, outcome, trace)
- [x] Rebalance: template CSV download; execution progress per row; per-lot FIFO breakdown
- [x] Kite session: order-mode line honest in live mode; time remaining; header chip

## P3 — completeness and polish

- [x] Model portfolio: KPIs (universe, weight sum, max price, out of reach), last price and last bought columns, footer weight sum
- [x] Config: API key (masked) and postback URL display, last change KPI
- [x] Ledger share side: running total, price/value, totals by source
- [x] Sign in: left information panel; attempts left on a wrong code
- [x] Enrol: download recovery codes as .txt (built from the page, never fetched again)
- [x] Day detail audit trail: before/after columns
- [x] Alerts: severity levels on "worth knowing"

## Second pass — re-audit after the first round

- [x] Rebalance detail template had been mangled by a bulk edit (restored; guard test on every template's size)
- [x] Rebalance "After" subtracted sold shares twice on a running batch; totals line added
- [x] Today's "what is running" read the target from orders placed so far; now the plan (sheet quantity, or the sell row)
- [x] Sell targets on the trading board came from orders placed so far; now the uploaded sell row
- [x] A past day's instrument page priced itself from today's market
- [x] Depth ladder marks a resting sell among the asks
- [x] Subscription months: billed on day one, then once per elapsed month (was rounded)
- [x] Model portfolio: refused upload kept the summary and history; names not held priced from the latest ranking
- [x] Kite session: an invalid token is never "in use now"; revoked sessions say why
- [x] Share ledger: range links keep the instrument; price and value columns
- [x] Cash ledger: available column
- [x] Holdings: day change against the previous stored close
- [x] Main: blotter counts, buy and sell filters
- [x] Instrument: the day's timeline reaches the ask and the settlement
- [x] Day detail: closing line (last tick, fallback or not), carried-into link
- [x] Today and the tradesheet link to Add lumpsum; recent days open the day
- [x] Sign-in footer, security page label ids, rebalance list N+1, order-mode text in live

## Not doing, and why

- Two-step sign-in, inline weight steppers, the 64-chip tick grid with Apply/Revert: replaced on purpose by forms that require a reason and are audited.
- Live-updating depth: the page shows the book as each tick stored it. A live ladder needs a websocket to the browser, which a server-rendered app without Redis does not have.
- OHLC and volume under the depth ladder: not stored.
- Fill source as postback or poll: not stored on the fill; the page says paper or broker.
- Tomorrow's projection in the lumpsum band ("LT 13 @ … overshoots"): it would rank
  tomorrow against today's prices, which is a guess the page would present as a plan.
- Trace column in configuration history: a configuration version carries no trace id.
- Web first-run enrolment: enrolling needs a session, and a session needs a
  confirmed authenticator. `enrol_totp` on the box is the root of trust on
  purpose; the replace flow on Security covers every later change.
- "I have saved these" checkbox gating Finish: without JavaScript it gates
  nothing, and the codes are gone the moment the page is left either way.
- Print button for recovery codes: needs JavaScript; the browser's own print
  and the .txt download cover it.
