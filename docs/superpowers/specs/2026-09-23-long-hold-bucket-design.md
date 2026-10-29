# steadystack — the long-hold bucket

**Date:** 2026-09-23
**Status:** Draft for review
**Relates to:** `docs/superpowers/specs/2026-09-21-steadystack-prd.md`

---

## 1. Purpose

steadystack has always had target weights. It has never had a written reason for
them. This document is that reason.

It defines the **selection policy** — what a name must be to enter the model
portfolio, what must happen before one leaves, and how often the question is
asked. It also records the bucket that policy produced on 2026-09-23, and the
candidates it rejected.

The distinction matters more than anything else here. **The policy is the
durable output; the weights are a photograph.** Prices, multiples and
quarterly numbers in §7 were taken on one day, in the middle of a correction.
Re-derive them before acting. Do not re-derive the policy without a reason.

## 2. Scope

**This changes no code.** The bucket *is* the model portfolio, not a second
sleeve — so the output is a `symbol,target_pct` CSV uploaded through the
existing model-portfolio screen, and every invariant in the PRD stands
untouched.

Explicitly out of scope: multi-portfolio support, per-row bucket tags,
automated sell suggestions, and any tax treatment. Each was considered and
each was declined (§4, `L1` and `L6`).

One thing this document does **not** change but does newly depend on: the
system has no way to record a dividend (§8). The bucket yields 1.94%, so that
gap becomes live the day the first weights are uploaded.

## 3. Context

Three constraints shaped every decision below, and all three come from the
system rather than from a view about markets.

**The engine buys whole shares of exactly one name per day.** At the
configured ₹2,600/day that is roughly ₹6.5 lakh a year across ~250 trading
days. Per-share *price* is therefore a hard selection constraint, not a
detail.

**`M15d` starves expensive names.** A name priced above about five days'
budget — **₹13,000** at the current daily amount — is only ever bought out of
an accumulated reserve, and spends days unbuyable while carry builds. Any name
above that line is effectively excluded whatever its merits.

**The engine rebalances with new money and never sells to do it** (PRD §6.2).
This is an unusually good property — continuous reweighting with no disposal —
but it is quietly *anti-momentum*: it buys whatever has lagged and starves
whatever has won. A compounding strategy works by letting winners run, so the
two are in tension. §8 states how that is handled.

## 4. Decisions

| # | Question | Decision |
|---|---|---|
| L1 | What is the bucket? | The model portfolio itself. Not a second sleeve, not a tagged subset. No schema change, no new code. |
| L2 | What may it hold? | Single stocks, **plus listed ETFs where no single stock can express the exposure**. This amends PRD §5.4 — see §5.4 below for why the amendment is narrow and what it does not open. Index ETFs remain excluded: an index *can* be expressed in stocks, so §5.4's intent still bites there. |
| L2a | Gold | **10% in GOLDBEES.** Rationale, vehicle choice and admission criteria in §5.4. |
| L3 | What does it optimise for? | Business durability. Fewest occasions on which a company forces a sell. Refined by the low-volatility evidence in §5, then verified name by name against the financials and the last two earnings calls. |
| L4 | How many names? | 14 equities plus the gold sleeve. No single position above 10%. Enough that one failure is survivable; few enough to actually read every quarter. |
| L5 | Price ceiling | No name priced above five days' budget (₹13,000 today). Derived from `M15d`, re-derived whenever the daily amount changes. |
| L6 | Selling | Buy-only for now. This is a **preference to minimise sells, not a prohibition** — the operator has said so explicitly. §9 states what would trigger one. |
| L7 | Evidence required | Financials **and** the last two earnings calls. Neither alone is sufficient; §6 records why. |
| L8 | How gates apply | To *operating* economics. Where a consolidated lending subsidiary or genuine capital intensity makes a ratio non-comparable, the **trend in ROE and the direction of operating cash flow decide** instead. Used twice, both recorded in §7. |
| L9 | Valuation | Sizes a position; does not gate one. An expensive name gets a smaller weight, not a rejection. |
| L10 | Review cadence | Annual for the full policy; quarterly read of every holding's call. Weights are re-derived at review, not continuously. |
| L11 | Sector concentration | No sector above **30%**, measured on the whole portfolio. Staples sits at 23% after the gold sleeve. A sector cap exists because §7.3 shows concentration migrates rather than disappears when you fix it in one place. |
| L12 | Contribution indexation | The daily amount is **raised by inflation at each annual review**. §5.3 shows this is worth ~32% of the terminal real corpus — more than every selection decision in this document combined. |

## 5. Why durability, and what the evidence says

A *never-sell* bucket is not achievable with individual stocks over sixteen
years. The Sensex launched in January 1986 with thirty names: **seven are
still in the index forty years later, and only three — Reliance, Hindustan
Unilever and ITC — have never once exited.** Everything else was replaced,
demerged, acquired or fell out. Two of those three survivors are in this
bucket, which is pleasing and proves nothing.

What *is* achievable is that **sells are rare and triggered by the business
deteriorating rather than by the price moving** — so the policy's real job is
to minimise the number of times a company forces the operator's hand, not to
pretend the number can be zero. Budget for roughly one forced exit every two
to three years and the design stays honest.

Four approaches were compared against Indian data (Dec 2006 – Jun 2025):

| Strategy | CAGR | Max drawdown | 2008 recovery |
|---|---|---|---|
| **Low volatility** | **13.03%** | **−44.04%** | **7 months** |
| Momentum | 13.88% | −65.99% | 16 months |
| Sequential (low-vol → momentum) | 13.90% | −59.06% | 10 months |
| Nifty 50 | ~10.50% | −55.12% | **60 months** |

Low volatility is the only one that improves **both** sides — higher return
*and* shallower drawdown than the index — and it achieves it through *what it
owns* rather than *when it trades*. Every approach that cuts drawdown further
does so by moving to cash, which costs too much: a 50/200-day crossover on the
Nifty (2010–22) held drawdowns down but returned **4.01% against
buy-and-hold's 9.90%**. Six points of CAGR is not a reasonable price for
avoiding a fall there are sixteen years to absorb.

Momentum is rejected on the operator's own criterion: a **−66%** drawdown is
worse than the index it is meant to improve on.

The bucket therefore starts from the low-volatility cohort and is then
filtered on business quality — because **low volatility measures how a share
price behaves, not how a business performs**. Five of the thirty names in that
index failed on their financials (§7.2).

**Caveat on the table above.** The CAGR and drawdown figures come from a
single published backtest and have not been independently reproduced. The
*direction* is corroborated elsewhere — the Nifty 100 Low Volatility 30 has
returned about 15.2% over fifteen years at 14% annualised volatility against
the Nifty's 16.5% — but treat the precise numbers as indicative. The decision
rests on the direction being robust, not on the second decimal place. Note
also that the quality and low-volatility factors have both **underperformed
recently**: Nifty200 Quality 30 returned 4.88% CAGR over five years and −7.51%
over one. This policy is being adopted while the style is out of favour, which
is either the opportunity or the trap, and the document should not pretend to
know which.

### 5.1 What this is expected to produce, and what it risks

₹2,600/day for sixteen years contributes **₹1.04 crore** of the operator's own
money regardless of outcome.

| CAGR | Corpus | Gain |
|---|---|---|
| 8% | ₹2.05 cr | ₹1.01 cr |
| **10%** | **₹2.45 cr** | ₹1.41 cr |
| **12%** | **₹2.94 cr** | ₹1.90 cr |
| **14%** | **₹3.54 cr** | ₹2.50 cr |
| 16% | ₹4.27 cr | ₹3.23 cr |

Planning band: **10–14%, i.e. ₹2.4–3.5 crore.** This is a range, not a
forecast, and no part of the design depends on landing inside it.

**The dominant risk is not stock selection. It is when a crash arrives.**
Assuming a −50% fall somewhere in the sixteen years:

| Crash in | Ends at 16y | vs no crash |
|---|---|---|
| Year 3 | ₹2.43 cr | **−17%** |
| Year 8 | ₹1.89 cr | −36% |
| Year 13 | ₹1.59 cr | −46% |
| Year 16 | ₹1.47 cr | **−50%** |

An early crash costs almost nothing — the system spends years buying cheap. A
late one costs half of everything, with no time to buy through it. **Sequence
risk at the end is the largest single threat to this corpus, and no choice of
names affects it.** It is addressed in §9 and remains partly open (§10).

### 5.2 Every number above is nominal, and that flatters them

India's CPI has averaged roughly **6% since 2000** and about **4.8% since the
RBI adopted inflation targeting in 2016**, against a 4% target with a 2–6%
band. At an assumed **5%**, money loses more than half its value over sixteen
years — the deflator is **2.18×**.

| Nominal CAGR | Real CAGR | Nominal corpus | **Real, in today's rupees** |
|---|---|---|---|
| 8% | 2.9% | ₹2.05 cr | **₹0.94 cr** |
| 10% | 4.8% | ₹2.45 cr | **₹1.12 cr** |
| **12%** | **6.7%** | **₹2.94 cr** | **₹1.35 cr** |
| 14% | 8.6% | ₹3.54 cr | **₹1.62 cr** |
| 16% | 10.5% | ₹4.27 cr | **₹1.95 cr** |

The planning band of ₹2.4–3.5 crore is therefore **₹1.1–1.6 crore in today's
money**. That is the number to hold in mind when judging whether this is
enough for its purpose. It is a very different sentence from "₹3 crore".

**The contribution decays too, and this is the part that is easy to miss.**
₹2,600/day held flat is worth **₹1,191/day in today's terms by year sixteen**
— a 54% erosion. The system would be contributing less than half as much, in
real terms, in the years when the corpus is largest and compounding hardest.

### 5.3 Indexing the daily amount is the highest-leverage decision here

Raising the daily amount by inflation each year — about **₹130 in the first
year**, adjusted at each annual review — changes the outcome by more than any
selection decision in this document:

| Nominal CAGR | Fixed ₹2,600 | Indexed 5%/yr | Gain |
|---|---|---|---|
| 10% | ₹1.12 cr real | **₹1.51 cr real** | **+34%** |
| 12% | ₹1.35 cr real | **₹1.78 cr real** | **+32%** |
| 14% | ₹1.62 cr real | **₹2.10 cr real** | **+30%** |

Roughly **a third more corpus, in real terms**, for a change that requires no
code, no new names, and no view about markets — only that the operator's
income keeps pace with inflation, which over sixteen years is the ordinary
expectation.

For comparison: the entire low-volatility versus index decision in §5 is worth
about 2.5 points of CAGR. Indexation is worth 30%+ of the terminal real
corpus. **Stock selection is the interesting problem; contribution indexation
is the important one.** A document that spends nine sections on the first and
none on the second would be misleading about where the leverage sits.

This is recorded as `L12` and belongs in the annual review checklist.

### 5.4 Gold, and the narrow amendment that admits it

PRD §5.4 bars "mutual funds in any form". A gold ETF is technically a mutual
fund unit, so on the letter it is barred. On the intent it is not, and the
distinction is worth stating once so this is not re-argued.

**§5.4 exists to keep mutual-fund plumbing out** — Coin's separate API,
folios, NAV cutoffs, fractional units, no order book, no depth, no tick feed.
Every mechanism this system is built on would need a parallel implementation.
**A listed ETF bypasses all of it.** GOLDBEES is an NSE cash-segment
instrument: in the Kite instrument master, live order book and depth, on the
tick websocket, CNC settled, whole units, its own tick size. The ladder prices
it from depth exactly as it prices a share; `Lot` holds integers; FIFO is
untouched. **The amendment costs zero engineering.**

The amendment is therefore narrow: **a listed ETF is permitted only where no
single stock can express the exposure.** Gold qualifies — India has no
meaningful listed gold miner, Sovereign Gold Bonds are no longer issued fresh
and trade thinly, and jewellers and gold financiers are businesses exposed to
gold *demand*, not to gold. An index ETF does not qualify, because an index
can be expressed in stocks, and §5.4's intent still applies.

**Why 10%.** Indian data puts the optimal range at 7.5–15%. Gold's correlation
to Indian equity is **−0.08 to −0.13**, so a small weight does real work.
Sharpe improves **1.76 → 1.95** moving from zero to 10% gold, then only to
2.01 at 20% — nearly all the benefit arrives by 10%. An 80:20 equity-gold mix
showed roughly **−30% maximum drawdown against pure equity's −44%**.

Beyond 10% you buy diminishing protection with a permanent drag: gold earns
nothing — no dividend, no reinvestment, no compounding. At roughly 9% nominal
for gold in rupees against 12% for the equity sleeve, **10% gold costs about
0.3pp of portfolio CAGR**, or ₹12–14 lakh of terminal corpus. That is the
price of the drawdown reduction, and it is paid deliberately.

**Why GOLDBEES and not a cheaper one.** ICICI Prudential Gold ETF charges
0.49% against GOLDBEES's 0.69–0.81%. Normally the cheaper one wins. Here it
does not, because **`M20` prices every rung from the live book and `M20a`
places nothing on a thin or one-sided one** — a ladder against a shallow ETF
book would spend days placing nothing at all. GOLDBEES's ₹59,000 cr AUM buys
the deepest book and tightest spreads on the NSE. The extra ~0.2% on a 10%
sleeve is about **₹24,000 a year on a ₹2.94 crore corpus**, paid for
fills that actually happen.

**Admission criteria for the gold sleeve**, because §6's gates are meaningless
here — there is no ROCE, no earnings call, no promoter and no moat:

| Test | Requirement |
|---|---|
| Backing | Physically backed by allocated gold, not derivatives or fund-of-funds |
| Depth | AUM large enough that the ladder fills; GOLDBEES is the largest in India |
| Tracking error | Below 0.5%; currently 0.28–0.40% |
| Expense ratio | Disclosed, monitored annually, currently 0.69–0.81% |
| Venue | NSE cash segment, in the instrument master, on the tick feed |

Reviewed annually with everything else. Switch vehicles only if another ETF
beats it on **both** expense and depth — not on expense alone, for the reason
above.

**The engine rebalances equity against gold for free.** When equities fall and
gold rises, gold goes overweight and equities underweight, so the gap ranking
sends the daily buy into equities — into the asset that just got cheaper.
When gold lags, it buys gold. No sells, no timing, no decision required. This
is the one place where §3's anti-momentum drift is exactly the behaviour
wanted, and it is the strongest argument for holding gold *inside* the model
portfolio rather than beside it.

## 6. The gates

Run on ten years of history, consolidated, not on a single year.

| Gate | Test | Why |
|---|---|---|
| Returns on capital | ROCE > 18–20%, stable year to year. Banks excluded — ROE > 15% instead | One good year is luck; a decade of it is structural |
| Balance sheet | Borrowings below reserves; interest cover > 5×. Net cash preferred | Leverage converts a bad three years into a permanent loss |
| **Cash conversion** | Operating cash flow positive every year and **rising**; CFO/EBITDA > 70% across the decade | **The gate that earns its place.** Profit that never becomes cash is usually not profit |
| Growth | Revenue CAGR > 10%, and consistency across the ten years | Separates a compounder from a high-return business that has stopped |
| Franchise | The moat must be expressible in one sentence: brand, distribution, switching cost, licence, network | If you cannot say why the return persists, it will not |
| Governance | Promoter pledging ≈ 0, clean related-party dealings, no auditor resignations, no serial dilution | Where Indian portfolios actually die — not from bad economics, from bad people |
| **Management commentary** | Last two earnings calls read in full | §6.1 |

### 6.1 Why the calls are a gate and not a courtesy

On 2026-09-23 the financials alone produced **two wrong judgements** out of
fourteen:

- **Hindustan Unilever** failed on operating cash flow declining three
  straight years (₹15,469 → ₹11,886 → ₹10,999 cr) and three-year sales growth
  of 2%. The Q1 FY27 call reported **underlying sales growth of 10%, the
  fastest in thirteen quarters**, with 5% volume growth. The books were
  describing a period that had ended.
- **Cummins India** was added specifically to diversify *away* from the AI
  thesis. The call disclosed that **data centres are 40% of domestic powergen
  revenue, up from 23%** — it is an AI-capex play, on the opposite side of the
  same trade.

Conversely the calls confirmed risks the books only hinted at. **Infosys**
management named **"AI-led deflation"** in its own words and cut FY27 guidance
to **1.5–3%** constant currency, citing clients demanding that AI productivity
gains be passed through as price cuts. **ITC**'s stalled numbers were
explained by the GST Compensation Cess expiring and excise rising from 1
February 2026 — cigarette segment pre-tax profit **−31.5% YoY** — which
reframes a stall as a recurring regulatory ratchet.

**Rule: no name is added, dropped, or re-weighted without reading its last two
earnings calls.** The books say what a business has earned. The call is the
only place management must answer for what happens next.

### 6.2 When the books and the call disagree

They will, and the bucket already contains one case of each direction. The
resolution rule is **the more recent evidence wins on the question of
direction; the older evidence still wins on the question of quality.**

- A failed gate plus a call showing genuine inflection means **hold at reduced
  weight and re-test next quarter** — not a clean pass. **HINDUNILVR** is held
  at 6% on exactly this basis: its ten-year economics are excellent, its
  three-year cash flow is not, and one strong quarter is evidence of a turn
  rather than proof of one. If two more quarters confirm, the weight goes
  back up; if they do not, it goes.
- A passed gate plus a call showing deterioration means **trim now**, because
  guidance leads the financials. **INFY** and **HDFCBANK** were both cut on
  call evidence while their books still read acceptably.

This is why **HCLTECH** could be cut for falling operating cash flow while
HINDUNILVR is held with the same flaw: HCL's call offered nothing that
contradicted the trend, and HUL's did. The gate is not applied mechanically —
but the override must always be *named*, as it is here and in `L8`, so that a
future reader can see a judgement was made rather than a rule forgotten.

## 7. The bucket as at 2026-09-23

Derived with the Nifty at 23,329, roughly **14% below its January 2026 peak**
of 26,373, with FPI ownership at a 14-year low. These are correction prices.

### 7.1 Holdings

| Symbol | Weight | Price | Days' budget | P/E | Yield | Sector |
|---|---|---|---|---|---|---|
| ICICIBANK | 9% | ₹1,340 | 0.5d | 17.2 | 0.90% | Financials |
| BHARTIARTL | 9% | ₹1,817 | 0.7d | 36.3 | 1.32% | Telecom |
| TCS | 8% | ₹2,105 | 0.8d | 14.2 | 3.04% | IT |
| ITC | 7% | ₹266 | 0.1d | 16.8 | 5.46% | Staples |
| M&M | 7% | ₹3,044 | 1.2d | 19.7 | 1.08% | Autos |
| SUNPHARMA | 7% | ₹1,846 | 0.7d | 35.1 | 0.87% | Pharma |
| NESTLEIND | 6% | ₹1,364 | 0.5d | 70.3 | 0.88% | Staples |
| PIDILITIND | 6% | ₹1,573 | 0.6d | 60.4 | 0.73% | Materials |
| HDFCBANK | 6% | ₹739 | 0.3d | 14.4 | 1.76% | Financials |
| INFY | 6% | ₹1,029 | 0.4d | 13.4 | 4.66% | IT |
| BRITANNIA | 5% | ₹4,918 | 1.9d | 45.5 | 1.84% | Staples |
| HINDUNILVR | 5% | ₹1,934 | 0.7d | 41.1 | 2.12% | Staples |
| CUMMINSIND | 5% | ₹4,962 | 1.9d | 56.5 | 1.33% | Industrials |
| ASIANPAINT | 4% | ₹2,448 | 0.9d | 48.0 | 1.12% | Materials |
| **GOLDBEES** | **10%** | **₹125.57** | **0.05d** | — | — | **Gold** |

**Equity 90% / gold 10%.** Within the equity sleeve: Staples 23%,
Financials 15%, IT 14%, Materials 10%, Telecom 9%, Autos 7%, Pharma 7%,
Industrials 5%. Largest position 10% (gold), largest equity position 9%,
largest sector 23%. Most expensive name ₹4,962 against the ₹13,000 line —
**nothing is starved**, and GOLDBEES at 0.05 days is the cheapest unit in the
book by an order of magnitude.

Equity weights are the §7-derived figures scaled by 0.9 with largest-remainder
rounding, so the ordering established across §6 and §7 is preserved exactly.

Two names are held under the `L8` override, where the headline ratio is
structurally distorted:

- **BHARTIARTL** — borrowings exceed reserves, normal for telecom
  infrastructure. Borrowings are *falling* (₹2.16L → ₹1.95L cr), reserves
  nearly doubled, operating cash flow rose **55% in two years** to ₹1.22L cr,
  ROE climbed 12% → 20%.
- **M&M** — ROCE 15.1% is depressed by consolidating Mahindra Finance. ROE is
  **15 → 18 → 19 → 20%** and operating cash flow **−₹5,630 → ₹11,657 cr**.

### 7.2 Rejected, and why

Kept deliberately, because the reasons are the policy in action and a future
reader will otherwise re-litigate them.

| Name | Reason |
|---|---|
| **TITAN** | The case that justifies the cash-conversion gate. 37.7% ROE and +45% TTM sales, but borrowings **doubled in two years to ₹30,621 cr — twice its reserves** — and operating cash flow went **negative (−₹541 cr)** in FY25. Growth funded on the balance sheet, at 75× earnings |
| **BEL** | Same failure, different cause: CFO **₹4,659 → ₹1,541 cr** while profits grew; receivables 170 days; working capital 79 → 135 days. One slow-paying government customer |
| **HAL** | Working capital **153 → 301 days**, sales growth 7%, ₹3,897 cr of other income inflating earnings |
| **DRREDDY** | ROCE 13.0%, TTM profit **−43%**, borrowings ₹2,002 → ₹7,734 cr |
| **CIPLA** | ROCE 15.5%, ROE 11.6%, TTM profit **−34%**, promoter holding −4.24% over three years |
| **KOTAKBANK** | Clearest deceleration on the list: profit growth **19 → 14 → 9 → 5%**, ROE 11.4%, at 20.5× |
| **LT** | ROCE 14.6% below gate, borrowings ₹1.25L cr exceed reserves ₹1.09L cr, lumpy cash flow |
| **TRENT** | Excellent business — but **81× earnings with growth decelerating to 17%/15%** from a five-year 51%/69%. Paying that multiple for a decelerating grower is the opposite of this mandate |
| **HCLTECH** | Cut when IT was reduced: the only one of the three with **falling** operating cash flow (₹22,448 → ₹19,975 cr) |
| Index ETFs | Lower turnover and self-cleaning, but barred by PRD §5.4 (L2) |

### 7.3 The live concentration

**Staples at 23%** is the largest equity exposure and the expensive one —
Nestlé 70×, Pidilite 60×, Britannia 45×. It replaced an earlier **24% IT**
concentration that rested on a single thesis, and the swap cost about a point
of weighted multiple and 25bps of yield. That was a deliberate trade, not an
improvement. The gold sleeve then diluted it further, which was a side effect
rather than the reason for holding gold.

**IT at 15%** remains a live judgement. Infosys management has confirmed
pricing deflation; TCS reported $9.5bn of deal wins and a $2.6bn AI revenue
run-rate but no guidance. Both still generate large cash and pay 3–4.7%.
Neither should be increased without evidence the deflation has stopped.

## 8. Engine interaction

**Anti-momentum drift.** The engine buys the most underweight name, so a
holding that compounds is bought less and less. Left alone this trims winners
and funds laggards — the reverse of what a compounding strategy needs.
**Handled at annual review by raising the target weight of names whose thesis
has strengthened**, rather than by selling. This is the one place where the
review has to act deliberately instead of observing.

**Starvation.** `L5`'s ₹13,000 ceiling is a function of the daily amount. If
the daily amount changes, re-run the check against every holding before
uploading new weights. Dixon (₹13,084) was excluded from an earlier candidate
list on exactly this basis, having passed on every other count.

**Version pinning.** Every tradesheet points at the model-portfolio version
that produced it (PRD D14), so a weights revision is self-documenting. Upload
a new version; never edit one.

**Dividends have nowhere to go, and the obvious workaround is wrong.** This
bucket yields **1.94%** — on a ₹2.9 crore corpus that is over ₹5 lakh a year
arriving as cash in the broker account. `grep -ri dividend src/` returns
**nothing**. `ExternalSide` offers `BUY`, `SELL`, `TRANSFER_IN`,
`TRANSFER_OUT`; `DivergenceClassification` offers external buy, external sell,
transfer, corporate action and system error. A dividend credit matches none of
them.

**Record it as a lumpsum.** It is the correct interim, and the alternative —
not recording it — is worse, because the cash then sits at the broker
unexplained and fails the ledger's reconciliation identity (M40a).

What it costs is **reporting accuracy, not accounting accuracy**, and the
mechanism is worth stating precisely because the obvious guess is wrong. XIRR
does *not* read `contributed`; `returns.py` builds its flows from
`cash_spent + charges`. A lumpsum becomes spendable cash, is deployed, and
appears as an extra **negative** flow — with no matching positive flow, since
lumpsums never enter `contribution_flows` on the way in. That asymmetry strips
exactly the dividend component out of the measured return:

| | Correct | As lumpsum |
|---|---|---|
| Reported XIRR | 12.00% | **10.06%** |
| Dashboard `contributed` over 16y | ₹1.04 cr | **₹1.38 cr** |

**The error is exactly the portfolio's dividend yield** — reported XIRR plus
current yield ≈ true total return. The reported figure becomes price return
rather than total return.

What survives untouched: the cash ledger reconciles; lots, cost basis and
realised gains are unaffected; and — the part most likely to be assumed
broken — **the benchmark comparison stays fair**. `returns.py` mirrors the
same flows into index units, and `benchmark_level` comes off the tick feed,
which carries a price index rather than a TRI. Both sides therefore end up
measured price-only, so `beat_the_benchmark` is not distorted.

The proper fix is small: a distinct `ExternalSide`, excluded from both
`contributed` and the XIRR outflow. Worth doing so the headline number stops
needing a mental asterisk, but it **does not block the first upload**.

**Corporate actions change lots correctly but say nothing about weights.**
`records/corporate_actions.py` handles splits and consolidations properly —
lots are superseded, acquisition dates carried forward (M36b). But the *model
portfolio* is unaffected, and two cases need an operator decision:

- **A split or bonus halves the price** and therefore halves a name's days'-
  budget cost. That is harmless — it only ever relieves `L5` pressure — but it
  means the §7.1 price column ages faster than the rest of the table. HDFC
  Bank at ₹739 already reflects one.
- **A demerger creates a symbol that is not in the model portfolio.** The
  engine will hold shares it has no target weight for, and the gap ranking has
  no opinion about them. This must be resolved at the next review: either add
  the new entity with a weight, or sell it under §9. It should not be left
  drifting, because an untargeted holding silently distorts every other name's
  computed gap.

## 9. Sell doctrine

Buy-only today. The operator's position is that sells are to be **minimised,
not avoided** — so this section records what would justify one, decided while
calm rather than during a fall.

| Trigger | Test | Action |
|---|---|---|
| **Business deterioration** | A gate in §6 failed for **three consecutive quarters**, confirmed on the calls. One bad quarter is not a trigger | Rebalance-sell, full exit |
| **Governance** | Pledging appears, auditor resigns, related-party dealings change character | Immediate, no waiting period |
| **Thesis void** | The one-sentence moat can no longer be stated | Exit at review |
| **Weight rebalance** | A position exceeds **15% of portfolio value** through appreciation | Trim to 10% at annual review |
| **Sector breach** | A sector exceeds `L11`'s 30% through appreciation | Trim the most expensive name in it |
| **Delisting or acquisition** | Company is acquired, goes private, or is suspended | Forced — no judgement available. Record proceeds, redistribute the weight at the next review |
| **Glide path** | From approximately year 12 | See §10 — **parameters not yet decided** |

**The 15% figure is an absolute cap on realised weight, not a multiple of
target.** §8 permits raising a winner's *target* weight at review — but that
raised target may not exceed `L4`'s 10%, and the 15% trim trigger continues to
apply to actual portfolio weight regardless of what the target says. Without
this sentence the two sections contradict each other: §8 rewards winners by
letting them grow, §9 caps how far. Both are intended. The cap wins.

Every sell is a manual CSV upload through the `rebalance` app. PRD §5.4 keeps
automated sell suggestions out, and nothing here changes that.

**Expected frequency.** §5 budgets roughly one forced exit every two to three
years from deterioration, plus whatever delistings and acquisitions arrive
unbidden. If sells are running materially above that, the gates are too tight
or the names were wrong. If there have been none in five years, the gates are
probably not being applied.

## 10. Open questions

**Gold — settled**, see `L2a` and §5.4. 10% in GOLDBEES, admitted under a
narrow amendment to PRD §5.4 that permits a listed ETF only where no single
stock can express the exposure. One consequence is left open below.

**The PRD now disagrees with this document.** PRD §5.4 still reads "Mutual
funds in any form — no holdings, no weights, no upload. Stocks only." That
line is the source of truth for behaviour and it is now wrong. It should be
amended to carry `L2`'s narrower rule, or this spec will read as a violation
rather than an amendment to anyone who finds the PRD first.

**Glide-path parameters.** §5.1 shows this is the largest risk in the design,
and §9 reserves a row for it. What fraction moves out of equities, into what,
starting exactly when, and whether the engine or the operator executes it, are
all undecided. It is not urgent — it is twelve years away — but it should not
be discovered in year eleven.

**Dividend recording.** Lumpsum is the working interim (§8), so this blocks
nothing — but it leaves the headline XIRR reading price return rather than
total return, understated by exactly the dividend yield. The permanent fix is
a distinct `ExternalSide` excluded from both `contributed` and the XIRR
outflow. Small, and worth doing before the corpus is large enough that the
asterisk matters. Until then, **the performance screen should state that
reported XIRR excludes dividends**, because a number that needs a mental
correction and does not say so is worse than one that is simply wrong.

**Re-derivation.** Every price, multiple and quarterly figure here is from
2026-09-23, mid-correction. If this sits more than a quarter before being
acted on, re-run §7 rather than trusting it.

## 11. Success criteria

This policy is working when:

- A weights revision can be explained from this document alone, without
  reconstructing the reasoning.
- Sells are rare, and every one traces to a §9 trigger rather than to a price
  move.
- No holding is ever starved by `M15d`, because `L5` was checked before upload.
- Every holding's last two calls have been read within the quarter.
- A future reader can see why a rejected name was rejected without re-running
  the analysis — §7.2 answers it.
- The daily amount has been raised at every annual review (`L12`), because
  that is worth more than everything else on this list combined.
- Dividends reconcile. If the ledger's M40a identity fails by roughly 1.9% of
  portfolio value a year, §8 was never addressed.

## 12. Review checklist

The annual review, in order. It exists so the review is a procedure rather
than a mood.

1. **Raise the daily amount** by the past year's CPI (`L12`). Do this first,
   because it is the item most likely to be skipped and the one that matters
   most.
2. Re-derive `L5`'s price ceiling from the new daily amount, and check every
   holding against it.
3. Read the last four quarters of calls for all fourteen names (`L7`).
4. Re-run the §6 gates on updated financials.
5. Resolve anything §8 flagged — untargeted holdings from demergers, split-
   affected prices.
6. Check `L4` (no position above 10%) and `L11` (no sector above 30%) against
   *realised* weights, and apply §9 where breached.
7. Raise target weights for names whose thesis strengthened (§8), within
   `L4`'s cap.
8. Upload a new model-portfolio version. Never edit an existing one.
9. From year 12, review the glide path (§10) as its own item.
