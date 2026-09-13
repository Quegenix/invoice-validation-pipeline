"""
Deterministic validation rules for extracted invoice records.

This is the part of the pipeline that earns the fee.

Extraction is probabilistic — a model reads a page and tells you what it
thinks it says, with no calibrated sense of whether it's right. These rules
are the opposite: cheap, deterministic, and they answer a different question.
Not "did we read it correctly" but "is what we read internally consistent and
safe to post."

Four of the seven rules below catch documents that are perfectly legible and
still wrong. No better extractor would help. That is the argument for having
a validation layer at all.

Every rule returns a Finding with a severity:
    BLOCK  — never post unattended, a human must look
    WARN   — post is defensible, but somebody should know
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

BLOCK = "BLOCK"
WARN = "WARN"

# Tolerance for arithmetic comparisons. Real invoices occasionally round a
# tax line a cent differently than a naive recomputation would. One cent of
# slack stops that from generating noise; anything larger is a real defect.
CENT = Decimal("0.01")

STALE_AFTER_DAYS = 365
FUTURE_GRACE_DAYS = 1


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    detail: str = ""


@dataclass
class ValidationResult:
    findings: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity == BLOCK for f in self.findings)

    @property
    def disposition(self) -> str:
        return "REVIEW" if self.blocked else "POST"

    @property
    def codes(self) -> list:
        return [f.code for f in self.findings]


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _money(v) -> Decimal:
    d = _dec(v)
    return d if d is not None else Decimal("0")


def validate(record: dict, *, known_vendors: set, seen_keys: set,
             today: date) -> ValidationResult:
    """
    `record` is an extracted invoice — what the extractor believes the
    document says. `seen_keys` is mutated: it is the running ledger of
    (vendor, invoice_number) pairs already processed.
    """
    res = ValidationResult()

    # ---------------------------------------------------------------
    # 1. Line items must sum to the stated subtotal.
    #
    # This is the single highest-value rule in the file. It catches
    # transposed digits, dropped lines, and misread decimal points —
    # the bulk of what extraction actually gets wrong — without needing
    # any confidence score from the model.
    # ---------------------------------------------------------------
    items = record.get("line_items") or []
    if items:
        line_sum = sum(_money(i.get("amount")) for i in items)
        subtotal = _money(record.get("subtotal"))
        delta = (line_sum - subtotal).copy_abs()
        if delta > CENT:
            res.findings.append(Finding(
                "LINE_SUM_MISMATCH", BLOCK,
                "Line items do not sum to the stated subtotal",
                f"lines total {line_sum:,.2f}, subtotal says {subtotal:,.2f} "
                f"(off by {delta:,.2f})",
            ))

    # ---------------------------------------------------------------
    # 2. subtotal + tax + freight must equal the stated total.
    # Catches charges that appear on the page but never made it into
    # what the vendor is actually asking for — in either direction.
    # ---------------------------------------------------------------
    subtotal = _money(record.get("subtotal"))
    tax = _money(record.get("tax"))
    freight = _money(record.get("freight"))
    total = _money(record.get("total"))
    computed = subtotal + tax + freight
    delta = (computed - total).copy_abs()
    if delta > CENT:
        res.findings.append(Finding(
            "TOTAL_MISMATCH", BLOCK,
            "Subtotal plus tax and freight does not equal the stated total",
            f"{subtotal:,.2f} + {tax:,.2f} + {freight:,.2f} = {computed:,.2f}, "
            f"total says {total:,.2f} (off by {delta:,.2f})",
        ))

    # ---------------------------------------------------------------
    # 3. Vendor must resolve against the master list.
    # A document can be flawless and still be from someone you have no
    # relationship with. Only a lookup catches it.
    # ---------------------------------------------------------------
    vendor = (record.get("vendor_name") or "").strip()
    if not vendor:
        res.findings.append(Finding(
            "MISSING_VENDOR", BLOCK,
            "No vendor name could be read from the document",
        ))
    elif vendor not in known_vendors:
        res.findings.append(Finding(
            "UNKNOWN_VENDOR", BLOCK,
            "Vendor is not in the vendor master list",
            f"read as '{vendor}' — either a new vendor needing setup, a "
            f"misread name, or a document that should not be here at all",
        ))

    # ---------------------------------------------------------------
    # 4 & 5. Invoice number present, and not already seen for this vendor.
    # Duplicate payment is the expensive failure in AP. It is also the
    # one that is hardest to notice after the fact.
    # ---------------------------------------------------------------
    inv_no = (record.get("invoice_number") or "").strip()
    if not inv_no:
        res.findings.append(Finding(
            "MISSING_INVOICE_NO", BLOCK,
            "No invoice number found on the document",
            "nothing to duplicate-check against, so this cannot post unattended",
        ))
    else:
        key = (vendor.lower(), inv_no.lower())
        if key in seen_keys:
            res.findings.append(Finding(
                "DUPLICATE_INVOICE_NO", BLOCK,
                "This vendor and invoice number have already been processed",
                f"'{inv_no}' from {vendor} was seen earlier in this run — "
                f"posting both would pay the vendor twice",
            ))
        seen_keys.add(key)

    # ---------------------------------------------------------------
    # 6 & 7. Date sanity.
    # ---------------------------------------------------------------
    d = record.get("invoice_date")
    parsed = None
    if d:
        try:
            parsed = date.fromisoformat(str(d))
        except ValueError:
            res.findings.append(Finding(
                "UNPARSEABLE_DATE", BLOCK,
                "Invoice date could not be interpreted",
                f"read as '{d}'",
            ))
    else:
        res.findings.append(Finding(
            "MISSING_DATE", BLOCK,
            "No invoice date found on the document",
        ))

    if parsed:
        age = (today - parsed).days
        if age > STALE_AFTER_DAYS:
            res.findings.append(Finding(
                "STALE_DATE", BLOCK,
                "Invoice is more than a year old",
                f"dated {parsed.isoformat()}, {age} days ago — either a very "
                f"late submission or a wrong year, and it should not drop "
                f"into the current period unnoticed",
            ))
        if age < -FUTURE_GRACE_DAYS:
            res.findings.append(Finding(
                "FUTURE_DATE", BLOCK,
                "Invoice is dated in the future",
                f"dated {parsed.isoformat()}, {abs(age)} days ahead — "
                f"usually a wrong year on the vendor's template",
            ))

    # ---------------------------------------------------------------
    # Advisory checks. These do not block a post, but a human who is
    # already looking should see them.
    # ---------------------------------------------------------------
    if total > Decimal("25000"):
        res.findings.append(Finding(
            "LARGE_AMOUNT", WARN,
            "Invoice exceeds the unattended-posting threshold",
            f"{total:,.2f} — worth an approval regardless of whether the "
            f"document reconciles",
        ))

    if not items:
        res.findings.append(Finding(
            "NO_LINE_ITEMS", WARN,
            "No line items were extracted",
            "the arithmetic reconciliation could not be performed",
        ))

    return res
