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


class _SetShim:
    """Lets a caller keep passing a set where a dict is now wanted."""

    def __init__(self, s):
        self._s = s

    def __contains__(self, k):
        return k in self._s

    def get(self, k, default=None):
        return default

    def setdefault(self, k, v):
        self._s.add(k)
        return None


def _money(v) -> Decimal:
    d = _dec(v)
    return d if d is not None else Decimal("0")


def validate(record: dict, *, known_vendors, seen_keys, today: date,
             buyer_name=None, base_currency="USD",
             file_name=None) -> ValidationResult:
    """
    `record` is an extracted invoice — what the extractor believes the
    document says.

    `seen_keys` is mutated. It maps a key to the file that first carried it,
    so a duplicate can name its twin rather than merely asserting one exists.
    Two kinds of key live in it, tagged so they cannot collide:
        ("num", vendor, invoice_number)     the same invoice arriving twice
        ("amt", vendor, date, total)        a re-issue under a new number
    A set is still accepted for backwards compatibility; the messages are
    just less specific.
    """
    if isinstance(seen_keys, set):
        seen_keys = _SetShim(seen_keys)
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
    # An invoice's totals identity is not subtotal + tax. Real bills deduct
    # discounts before tax and add fees after it, and a rule that ignores
    # either fires on every invoice that has one. On a 50-document corpus of
    # ordinary service invoices, 16 carried a discount or a fee -- so the
    # narrow version of this rule would have been wrong about a third of them
    # while looking exactly as confident as it does when it is right.
    discount = _money(record.get("discount"))
    fees = _money(record.get("fees"))
    computed = subtotal - discount + tax + freight + fees
    delta = (computed - total).copy_abs()
    if delta > CENT:
        parts = [f"{subtotal:,.2f}"]
        if discount: parts.append(f"- {discount:,.2f} discount")
        if tax: parts.append(f"+ {tax:,.2f} tax")
        if freight: parts.append(f"+ {freight:,.2f} freight")
        if fees: parts.append(f"+ {fees:,.2f} fees")
        res.findings.append(Finding(
            "TOTAL_MISMATCH", BLOCK,
            "The invoice's own figures do not add up to its stated total",
            f"{' '.join(parts)} = {computed:,.2f}, "
            f"total says {total:,.2f} (off by {delta:,.2f})",
        ))

    # A deposit or part-payment already applied means the total and the amount
    # owed are different numbers, and posting the wrong one is wrong by the
    # deposit. Nothing on the page says which one the books want.
    paid = _money(record.get("amount_paid"))
    bal = _dec(record.get("balance_due"))
    # Keyed on a stated payment, not merely on balance != total. A balance
    # that disagrees with the total when nothing was paid is a totals problem,
    # and TOTAL_MISMATCH already owns that; firing here too would just add
    # noise to a document that is already held.
    if paid > 0:
        shown = f"{bal:,.2f}" if bal is not None else f"{(total - paid):,.2f}"
        res.findings.append(Finding(
            "PARTIAL_PAYMENT", WARN,
            "Invoice total and amount owed are different numbers",
            f"total {total:,.2f}, already paid {paid:,.2f}, balance {shown} — "
            f"which one posts is a decision about this vendor, not something "
            f"the document answers",
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
    # No master list is a real state, not an error: it is every first
    # engagement, before anyone has exported one. Flagging all fifty invoices
    # as unknown would be technically true and completely useless.
    if not vendors.names:
        if not vendor:
            res.findings.append(Finding(
                "MISSING_VENDOR", BLOCK,
                "No vendor name could be read from the document"))
    elif not vendor:
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
        key = ("num", resolved.casefold(), inv_no.casefold())
        if key in seen_keys:
            twin = seen_keys.get(key)
            res.findings.append(Finding(
                "DUPLICATE_INVOICE_NO", BLOCK,
                "This vendor and invoice number have already been processed",
                f"'{inv_no}' from {vendor} was seen earlier in this run"
                + (f" on {twin}" if twin else "")
                + " — posting both would pay the vendor twice",
            ))
        seen_keys.setdefault(key, file_name)

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
    # 9. The same amount from the same vendor on the same day, under a
    #    different invoice number.
    #
    # This is the duplicate the invoice-number check cannot see: a vendor
    # re-issuing an unpaid invoice under a fresh number. It needs no
    # identifier at all, and the three fields it does use are each
    # cross-checked by the arithmetic above — so a misread in them raises a
    # flag rather than passing quietly, which is exactly the property the
    # invoice number lacks.
    #
    # Two genuinely separate invoices can legitimately share vendor, date and
    # amount (a duplicated monthly charge), which is why this holds for a
    # human rather than rejecting outright.
    # ---------------------------------------------------------------
    if parsed and total and vendor:
        akey = ("amt", resolved.casefold(), parsed.isoformat(), str(total))
        if akey in seen_keys:
            twin = seen_keys.get(akey)
            res.findings.append(Finding(
                "SAME_AMOUNT_REDUPLICATE", BLOCK,
                "Same vendor, date and amount as another invoice in this run",
                f"{total:,.2f} from {resolved} dated {parsed.isoformat()}"
                + (f" also appears on {twin}" if twin else " appears twice")
                + f", under a different invoice number — a re-issue looks like "
                  f"this, and the invoice-number check cannot see it",
            ))
        seen_keys.setdefault(akey, file_name)

    # ---------------------------------------------------------------
    # 10. Credit lines.
    #
    # A negative line means this is not a plain bill. Posting it as one gets
    # the sign wrong somewhere, and what a credit should actually do depends
    # on the accounting system. Undefined behaviour is a reason to stop.
    # ---------------------------------------------------------------
    negatives = [(n, i) for n, i in enumerate(items, 1)
                 if (_dec(i.get("amount")) or Decimal(0)) < 0]
    if negatives:
        res.findings.append(Finding(
            "NEGATIVE_LINE", BLOCK,
            "Contains a credit line",
            "; ".join(f"line {n}: {i.get('description') or ''} "
                      f"{_money(i.get('amount')):,.2f}" for n, i in negatives)
            + " — this is a credit memo or a mixed document, not a plain bill",
        ))

    # ---------------------------------------------------------------
    # 11. Currency.
    #
    # Nothing downstream reads a currency code, so a foreign-priced invoice
    # would post at face value as though it were dollars. The error is the
    # exchange rate, silently.
    # ---------------------------------------------------------------
    cur = (record.get("currency") or base_currency).strip().upper()
    if cur != base_currency.upper():
        res.findings.append(Finding(
            "FOREIGN_CURRENCY", BLOCK,
            f"Priced in {cur}, not {base_currency.upper()}",
            f"total {total:,.2f} is {cur}; posting it unconverted would enter "
            f"the wrong amount by whatever the exchange rate happens to be",
        ))

    # ---------------------------------------------------------------
    # 12 & 13. Two checks that need fields the extractor does not yet
    # return. Written now and inert until it does, so that adding the
    # fields is the only remaining step.
    # ---------------------------------------------------------------
    rate = _dec(record.get("tax_rate_printed"))
    if rate is not None and subtotal:
        if rate > 1:                      # "6" or "6%" rather than 0.06
            rate = rate / Decimal(100)
        # Tax applies to what is actually being paid, so a discount comes off
        # BEFORE tax is figured. Taxing the gross subtotal fires on every
        # discounted invoice: on a 50-document corpus of ordinary service
        # bills it was wrong 7 times out of 7, and no synthetic test caught it
        # because the generator never produced a discount.
        expected = ((subtotal - discount) * rate).quantize(CENT)
        # Half a percent of the tax, not a penny. Tax is commonly computed per
        # line and summed, so it will not match subtotal x rate exactly -- a
        # few cents out on a four-figure invoice is arithmetic, not a defect.
        # Measured: at a one-cent tolerance this fired on discrepancies of
        # 0.02 and 0.14, while every real case was out by 120.00 or more.
        tol = max(CENT, (expected * Decimal("0.005")).copy_abs())
        if (expected - tax).copy_abs() > tol:
            res.findings.append(Finding(
                "TAX_RATE_WRONG", BLOCK,
                "Tax charged does not match the tax rate printed on the invoice",
                f"{subtotal:,.2f} at {rate * 100:.3f}% is {expected:,.2f}, "
                f"but the invoice charges {tax:,.2f}",
            ))

    billed = (record.get("bill_to_name") or "").strip()
    if billed and buyer_name:
        if normalize_vendor(billed) != normalize_vendor(buyer_name):
            res.findings.append(Finding(
                "WRONG_BILL_TO", BLOCK,
                "Invoice is addressed to a different company",
                f"billed to '{billed}', not '{buyer_name}' — this is somebody "
                f"else's invoice and paying it is money gone",
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
