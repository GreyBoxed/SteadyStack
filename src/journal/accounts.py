"""The chart of accounts (spec §4).

Codes rather than names are what the rest of the system refers to, because a
name is copy and a code is an identifier. The chart is fixed: accounts are
installed by a data migration and never deleted, so a report joining to them
can never lose a row.
"""

UNALLOCATED = "1100"
ENGINE_CASH = "1110"
SETTLE_ENGINE = "1200"
SETTLE_OTHER = "1210"
INVESTMENTS = "1300"
CONTRIBUTIONS = "3100"
WITHDRAWALS = "3200"
OPENING = "3300"
PAPER_FUNDING = "3900"
REALISED = "4100"
DIVIDENDS = "4200"
ACCOUNT_CHARGES = "5100"

#: What the engine may spend is the balance of these two (spec §4).
ENGINE_ACCOUNTS = (ENGINE_CASH, SETTLE_ENGINE)

#: Zerodha's cash, as the CA reconciles it.
ZERODHA_ACCOUNTS = (UNALLOCATED, ENGINE_CASH, SETTLE_ENGINE, SETTLE_OTHER)

#: Accounts that may only appear in the paper book.
PAPER_ONLY = frozenset({PAPER_FUNDING})

CHART = (
    (UNALLOCATED, "Zerodha cash: unallocated", "asset"),
    (ENGINE_CASH, "Zerodha cash: engine", "asset"),
    (SETTLE_ENGINE, "Broker settlement: engine", "asset"),
    (SETTLE_OTHER, "Broker settlement: other", "asset"),
    (INVESTMENTS, "Investments", "asset"),
    (CONTRIBUTIONS, "Contributions", "equity"),
    (WITHDRAWALS, "Withdrawals", "equity"),
    (OPENING, "Opening balance", "equity"),
    (PAPER_FUNDING, "Paper funding", "equity"),
    (REALISED, "Realised gains", "income"),
    (DIVIDENDS, "Dividends", "income"),
    (ACCOUNT_CHARGES, "Account charges", "expense"),
)
