# Screens

Hi-fi design for the SteadyStack operator interface. Fifteen artboards, each a
self-contained `.dc.html` page, plus `canvas.json` — the index that gives each
board its frame on the canvas, the reading order, and the annotations.

**The canvas is the source of truth, not this folder.** These files are a
snapshot taken on 22 Sep 2026. Edits made on the canvas will not appear here
until someone copies them down again.

## Getting in

| File | Screen |
|---|---|
| `SignIn.dc.html` | Password, then TOTP. Four states, all reachable by clicking: password, code, wrong code, locked out. Shows nothing about the portfolio — M2. |
| `Enrol.dc.html` | First run. QR pairing, the secret shown once, verification before TOTP is enforced, and ten recovery codes gated behind an acknowledgement. |
| `KiteSession.dc.html` | The daily Kite connection. What crosses which boundary during the token exchange, the static-IP gate, and what stops working without a session. Press Re-connect to see the no-session state. |
| `AwaitingSession.dc.html` | A tradesheet parked at `AWAITING_SESSION`. Connect before 15:00 and the ladder resumes at the tick the clock implies; after it, the day is `SKIPPED` and the full amount carries. |

## Running the day

| File | Screen |
|---|---|
| `Today.dc.html` | Command deck — session, health, the daily cycle, what is working, alerts. |
| `Tradesheet.dc.html` | The frozen ranking, the lumpsum band, and the operator overrides: quantity, run/skip, per-tick exclusion. |
| `Main.dc.html` | Every instrument with orders today, and the day's blotter. |
| `Instrument.dc.html` | One instrument: each order as a lifespan bar, the depth ladder, per-tick and fill views. |
| `DayDetail.dc.html` | Any past day rebuilt from stored data alone, under one trace id. |

## On a phone

390x844. The minimum that has to work away from a desk.

| File | Screen |
|---|---|
| `MobileSignIn.dc.html` | Password then code. The authenticator is on the same phone, so the form survives you leaving the app to fetch it. |
| `MobileToday.dc.html` | The whole phone app in one screen: waiting for you, running, or something wrong. Tap the primary button to walk the day forward. |
| `MobileTradesheet.dc.html` | Review and authorise. Quantity stepper, the ranking that justified the pick, how stale the sheet is, and Regenerate. |
| `MobileKite.dc.html` | Connect for the day, and what stays blocked until you do. |
| `MobileStop.dc.html` | Halt the day. Two lists, the second being what it deliberately does not do. |

## Records, portfolio and setup

| File | Screen |
|---|---|
| `Ledger.dc.html` | Cash in and out, shares in and out, including trades made directly in Kite. |
| `Holdings.dc.html` | Weights against target, sorted by gap. |
| `Performance.dc.html` | XIRR and the drag the data subscription puts on it. |
| `Rebalance.dc.html` | CSV-driven sells, validated, matched FIFO. |
| `Model.dc.html` | Target weights. |
| `Config.dc.html` | Everything configurable, each change audited. |

## Known stale

These predate the final spec and have not been reworked yet:

- The sidebar still reserves a slot for "Tax lots and gains". Tax was dropped.
- `Today` lists a "dry run mode" check. It should read the paper/live order mode.
- `Rebalance` and `Performance` still carry tax labelling.
- The screens assume a one-share slice that commits the day by tick 6, with no
  concentration cap, no reserve, and no opening-import surface.
