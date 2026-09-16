"""
Deterministic validation rules for extracted invoice records.

This is the part of the pipeline that earns the fee.

Extraction is probabilistic — a model reads a page and tells you what it
thinks it says, with no calibrated sense of whether it's right. These rules
are the opposite: cheap, deterministic, and they answer a different question.
Not "did we read it correctly" but "is what we read internally consistent and
safe to post."

Several of the rules below catch documents that are perfectly legible and
still wrong. No better extractor would help. That is the argument for having
a validation layer at all.

Every rule returns a Finding with a severity:
    BLOCK  — never post unattended, a human must look
    WARN   — post is defensible, but somebody should know
"""

import re
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

# Half a cent per unit. A vendor that prints a unit price rounded to two
# decimals but extends the line from more precision can be off by at most
# this much, and that is not a defect. Anything beyond it is.
HALF_CENT_PER_UNIT = Decimal("0.005")

STALE_AFTER_DAYS = 365
FUTURE_GRACE_DAYS = 1


_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l l c|ltd|limited|co|corp|corporation|company"
    r"|plc|lp|llp)\b")


def normalize_vendor(name: str) -> str:
    """
    Fold away the things that differ between two printings of the same
    vendor and never distinguish two different vendors: letter case,
    punctuation, '&' vs 'and', a leading 'The', and a corporate suffix.

    'EVERLINE FASTENER & HARDWARE'  ->  'everline fastener and hardware'
    'Joplin Air Handling Co.'       ->  'joplin air handling'
    """
    s = (name or "").casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^the\s+", "", s)
    s = _SUFFIXES.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


class VendorIndex:
    """
    Resolves a vendor name as read off a page to a name on the master list.

    Three tiers, all of them exact string identity on a progressively more
    forgiving normalization. There is deliberately no similarity score here.
    On the sets this has been measured against, a genuine variant resolves
    at tier 'spacing' or better while the nearest unrelated vendor pair
    scores around 0.5 on any sequence-similarity metric — so a threshold
    would be a tunable with nothing to tune it against, and a wrong guess
    would silently post one vendor's invoice against another's account.
    A name this cannot resolve deterministically is reported unresolved and
    a human decides.

    The squashed tier is skipped entirely if the master list itself has two
    vendors that collapse together, since resolution would be a coin flip.
    """

    def __init__(self, names):
        self.names = [n for n in names if n]
        self._exact = set(self.names)
        self._norm, self._squash = {}, {}
        for n in self.names:
            self._norm.setdefault(normalize_vendor(n), n)
            self._squash.setdefault(normalize_vendor(n).replace(" ", ""), n)
        self.squash_usable = len(self._squash) == len(set(self._norm.values()))

    def resolve(self, name: str):
        """Return (canonical_name, tier) or (None, None)."""
        raw = (name or "").strip()
        if not raw:
            return None, None
        if raw in self._exact:
            return raw, "exact"
        n = normalize_vendor(raw)
        if n in self._norm:
            return self._norm[n], "normalized"
        if self.squash_usable:
            hit = self._squash.get(n.replace(" ", ""))
            if hit:
                return hit, "spacing"
        return None, None


def _as_index(known_vendors):
    if isinstance(known_vendors, VendorIndex):
        return known_vendors
    return VendorIndex(known_vendors or [])


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    detail: str = ""


@dataclass
class ValidationResult:
    findings: list = field(default_factory=list)
    resolved_vendor: str = ""

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
    vendors = _as_index(known_vendors)

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
    # 2. Each line's quantity times unit price must equal its extension.
    #
    # The line-sum rule above only checks the column total, so two errors
    # on different lines that happen to offset will pass it. This rule
    # checks each line against itself and does not care about the column.
    #
    # It is the only rule here that catches a misread which leaves every
    # total on the page reconciling — the right amount payable against the
    # wrong line detail. Arithmetic the document asserts about itself, so
    # a failure is a fact, not a policy judgement.
    # ---------------------------------------------------------------
    bad_lines = []
    for n, item in enumerate(items, start=1):
        qty = _dec(item.get("quantity"))
        price = _dec(item.get("unit_price"))
        amount = _dec(item.get("amount"))
        if qty is None or price is None or amount is None:
            continue          # line does not print the parts; rule 1 covers it
        extended = (qty * price).quantize(CENT)
        gap = (extended - amount).copy_abs()
        tolerance = max(CENT, (qty.copy_abs() * HALF_CENT_PER_UNIT))
        if gap > tolerance:
            bad_lines.append(
                f"line {n}: {qty} x {price} = {extended:,.2f} but the line "
                f"reads {amount:,.2f} (off by {gap:,.2f})")
    if bad_lines:
        res.findings.append(Finding(
            "LINE_EXTENSION_MISMATCH", BLOCK,
            "A line's quantity times unit price does not equal its amount"
            + (f" ({len(bad_lines)} lines)" if len(bad_lines) > 1 else ""),
            "; ".join(bad_lines),
        ))

    # ---------------------------------------------------------------
    # 3. subtotal + tax + freight must equal the stated total.
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
    # 4. Vendor must resolve against the master list.
    #
    # A document can be flawless and still be from someone you have no
    # relationship with. Only a lookup catches it.
    #
    # The lookup is forgiving about how the name is printed and strict
    # about who it belongs to. A letterhead set in capitals, a trailing
    # 'Co.', 'Sheetmetal' written as 'Sheet Metal' — those are the same
    # vendor and resolving them is the system's job, not the bookkeeper's.
    # What posts is the canonical name, so the ledger stays single-valued
    # however the vendor chose to print it that month.
    # ---------------------------------------------------------------
    vendor = (record.get("vendor_name") or "").strip()
    resolved = vendor
    if not vendor:
        resolved = ""
        res.findings.append(Finding(
            "MISSING_VENDOR", BLOCK,
            "No vendor name could be read from the document",
        ))
    else:
        canonical, tier = vendors.resolve(vendor)
        if canonical is None:
            res.findings.append(Finding(
                "UNKNOWN_VENDOR", BLOCK,
                "Vendor is not in the vendor master list",
                f"read as '{vendor}' — either a new vendor needing setup, a "
                f"misread name, or a document that should not be here at all",
            ))
        else:
            resolved = canonical
            if tier != "exact":
                res.findings.append(Finding(
                    "VENDOR_NAME_VARIANT", WARN,
                    "Vendor name is printed differently from the master list",
                    f"read as '{vendor}', posting against '{canonical}' "
                    f"(matched on {tier}) — same vendor, no action needed, "
                    f"recorded so the resolution is auditable",
                ))

    # The canonical name is what any downstream write should use. Putting it
    # on the record means results.json and every adapter see it for free.
    record["vendor_name_resolved"] = resolved
    res.resolved_vendor = resolved

    # ---------------------------------------------------------------
    # 5 & 6. Invoice number present, and not already seen for this vendor.
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
        # Keyed on the resolved name, not the printed one. Keying on what
        # the page happened to say would make 'GRANGER INDUSTRIAL GAS' and
        # 'Granger Industrial Gas' two separate ledgers and let the same
        # invoice through twice — the exact failure this rule exists for.
        key = (resolved.casefold(), inv_no.casefold())
        if key in seen_keys:
            res.findings.append(Finding(
                "DUPLICATE_INVOICE_NO", BLOCK,
                "This vendor and invoice number have already been processed",
                f"'{inv_no}' from {vendor} was seen earlier in this run — "
                f"posting both would pay the vendor twice",
            ))
        seen_keys.add(key)

    # ---------------------------------------------------------------
    # 7 & 8. Date sanity.
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
