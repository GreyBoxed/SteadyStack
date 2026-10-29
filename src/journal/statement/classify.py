"""What each statement line probably is (spec §7.2).

A proposal, never a decision: nothing posts until a person confirms it.
Unknown text -- including the wording of an owner-requested payout, which
Zerodha does not document -- is proposed as nothing and classified by hand.
"""

import re
from dataclasses import dataclass

from journal.models import Component


@dataclass(frozen=True)
class Proposal:
    kind: str
    component: str = ""
    reference: str = ""
    note: str = ""


REFERENCE = re.compile(r"reference number[:\s]+(\w+)", re.IGNORECASE)
SETTLEMENT = re.compile(
    r"^Net settlement for Equity with settlement number:\s*(\d+)", re.IGNORECASE
)

RULES = [
    (re.compile(r"^Funds auto-settled to the primary bank account", re.I), Proposal("sweep")),
    (re.compile(r"^Payout done for quarterly settlement", re.I), Proposal("sweep")),
    (re.compile(r"^AMC for Demat Account", re.I), Proposal("account_charge", Component.AMC)),
    (
        re.compile(r"^Delayed payment charges", re.I),
        Proposal("account_charge", Component.INTEREST),
    ),
    (
        re.compile(r"^Charges for enabling DDPI", re.I),
        Proposal("account_charge", Component.DDPI),
    ),
    (re.compile(r"^DP charges", re.I), Proposal("dp_charge", Component.DP)),
    (
        re.compile(r"^(Payment gateway|Call and trade) charges", re.I),
        Proposal("account_charge", Component.OTHER),
    ),
    (
        re.compile(r"^Kite Connect API Charges", re.I),
        Proposal("owner_expense", note="the owner's cost of automation (J7)"),
    ),
]


def propose(line) -> Proposal:
    text = line.particulars
    reference = (REFERENCE.search(text) or [None, ""])[1]

    settlement = SETTLEMENT.search(text)
    if settlement:
        return Proposal("settlement", reference=settlement.group(1))

    for pattern, proposal in RULES:
        if pattern.search(text):
            return Proposal(proposal.kind, proposal.component, reference, proposal.note)

    if line.voucher_type == "Bank Receipts" and line.credit > 0:
        return Proposal("contribution", reference=reference)
    if line.voucher_type == "Bank Payments" and line.debit > 0:
        return Proposal("withdrawal", reference=reference, note="confirm this payout by hand")
    return Proposal("unknown")
