#!/usr/bin/env python3
"""
Generate a set of fictitious vendor invoices as PDFs, in several visually
distinct layouts, with deliberately seeded data-quality failures.

Produces:
    out/invoices/*.pdf     the documents
    out/manifest.json      ground truth: what is printed on each document,
                           plus which failure (if any) was seeded into it
    out/vendors.csv        the known-good vendor master list
    out/README.md          what the set contains and what each case tests

Design note
-----------
The manifest records WHAT IS PRINTED ON THE PAGE, not what "should" have been
printed. That is deliberate. Extraction accuracy is measured against what a
human reading the document would transcribe. Whether those printed numbers are
internally consistent is a separate question, and that is exactly what the
validation layer exists to answer.

So for a seeded LINE_SUM_MISMATCH invoice, manifest["subtotal"] is the wrong
subtotal that appears on the document. A perfect extractor returns that value.
A correct validator then flags it.
"""

import json
import csv
import random
import io
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

from PIL import Image, ImageDraw, ImageFont, ImageFilter

import dataset
import fonts

SEED = 20260911
random.seed(SEED)

OUT = Path(__file__).parent / "out"
PDF_DIR = OUT / "invoices"

# ---------------------------------------------------------------------------
# Fonts
#
# Resolved per-platform rather than hardcoded. See fonts.py — run it directly
# to see what your machine provides. Roles that find no font file fall back to
# reportlab's built-in Type1 faces, so generation always completes.
# ---------------------------------------------------------------------------

F = fonts.register(pdfmetrics, TTFont)

DJSans = F["sans"]
DJSans_B = F["sans-bold"]
DJSerif = F["serif"]
DJSerif_B = F["serif-bold"]
DJCond = F["cond"]
DJCond_B = F["cond-bold"]
LibSans = F["sans2"]
LibSans_B = F["sans2-bold"]

# ---------------------------------------------------------------------------
# Money helpers — Decimal throughout. A demo about arithmetic validation has
# no business having float rounding artifacts of its own.
# ---------------------------------------------------------------------------

TWO = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWO, rounding=ROUND_HALF_UP)


# The currency a document is being rendered in. Set around each render, the
# same way dataset.BUYER is swapped for a wrong-bill-to document.
#
# This exists because a FOREIGN_CURRENCY defect used to set a field on the
# record that NO layout ever drew. The page showed plain dollar amounts, the
# extractor correctly read USD, and the answer key insisted the invoice was in
# CAD. A defect that is not on the page is not a defect -- it is a bug in the
# generator that scores the pipeline for a miss it never had a chance at.
CURRENCY = "USD"


def fmt(d: Decimal, symbol=False) -> str:
    s = f"{d:,.2f}"
    if not symbol:
        return s
    # A dollar sign on a pound amount is wrong, and a reviewer reading
    # "GBP $3,352.40" would rightly distrust the whole document.
    return f"${s}" if CURRENCY == "USD" else f"{CURRENCY} {s}"


# ---------------------------------------------------------------------------
# Date formats — deliberately varied, because real AP inboxes are varied
# ---------------------------------------------------------------------------

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DATE_FORMATS = ["us_slash", "iso", "abbr_dash", "long"]


def fmt_date(d: date, style: str) -> str:
    if style == "us_slash":
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    if style == "iso":
        return d.isoformat()
    if style == "abbr_dash":
        return f"{d.day:02d}-{MON_ABBR[d.month - 1]}-{d.year}"
    if style == "long":
        return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
    raise ValueError(style)


def due_date(issued: date, terms: str) -> date:
    if terms == "Due on Receipt":
        return issued
    if terms == "Net 15":
        return issued + timedelta(days=15)
    if terms in ("Net 30", "2/10 Net 30"):
        return issued + timedelta(days=30)
    return issued + timedelta(days=30)


# ---------------------------------------------------------------------------
# Invoice construction
# ---------------------------------------------------------------------------

ERROR_TYPES = [
    "LINE_SUM_MISMATCH",
    "TOTAL_MISMATCH",
    "DUPLICATE_INVOICE_NO",
    "UNKNOWN_VENDOR",
    "STALE_DATE",
    "FUTURE_DATE",
    "MISSING_INVOICE_NO",
]

TODAY = date(2026, 9, 11)


def make_line_items(vendor, n):
    cat = dataset.CATALOG[vendor["category"]]
    picks = random.sample(cat, min(n, len(cat)))
    repeats = 0
    while len(picks) < n:
        picks.append(random.choice(cat))
        repeats += 1

    items = []
    seen_desc = {}
    for desc, unit, lo, hi, qlo, qhi in picks:
        # A long invoice legitimately repeats a part across lots. Give repeats
        # a distinguishing lot reference rather than printing the same line
        # verbatim four times, which reads as generated data.
        seen_desc[desc] = seen_desc.get(desc, 0) + 1
        label = desc
        if seen_desc[desc] > 1:
            label = f"{desc}  [lot {random.randint(2200, 9800)}]"

        qty = random.randint(qlo, qhi)
        price = money(random.uniform(lo, hi))
        items.append({
            "description": label,
            "unit": unit,
            "quantity": qty,
            "unit_price": price,
            "amount": money(price * qty),
        })
    return items


def build_invoice(idx, vendor, layout, n_items, issued, seq):
    items = make_line_items(vendor, n_items)
    line_sum = money(sum(i["amount"] for i in items))

    tax_rate = Decimal(str(vendor["tax_rate"]))
    freight = money(random.uniform(24, 180)) if vendor["has_freight"] and random.random() < 0.7 else money(0)

    subtotal = line_sum
    tax = money(subtotal * tax_rate)
    total = money(subtotal + tax + freight)

    prefix = "".join(w[0] for w in vendor["name"].split()[:2]).upper()
    invoice_no = f"{prefix}-{seq:05d}"

    return {
        "file_id": f"INV-{idx:03d}",
        "vendor_name": vendor["name"],
        "vendor": vendor,
        "invoice_number": invoice_no,
        "invoice_date": issued,
        "due_date": due_date(issued, vendor["terms"]),
        "terms": vendor["terms"],
        "po_number": f"PO-{random.randint(41000, 41999)}" if random.random() < 0.6 else None,
        "line_items": items,
        "line_item_sum": line_sum,   # true sum of the printed line amounts
        "subtotal": subtotal,        # what gets PRINTED as subtotal
        "tax_rate": tax_rate,
        "tax": tax,
        "freight": freight,
        "total": total,              # what gets PRINTED as total
        "currency": "USD",
        "layout": layout,
        "date_format": random.choice(DATE_FORMATS),
        "seeded_error": None,
        "error_note": None,
    }


def seed_error(inv, kind, all_invoices):
    """Mutate an invoice so it carries exactly one data-quality defect."""
    inv["seeded_error"] = kind

    if kind == "LINE_SUM_MISMATCH":
        # Classic transposition in the subtotal: 1,234.56 printed as 1,243.56
        bad = money(inv["subtotal"] + Decimal("9.00"))
        inv["subtotal"] = bad
        inv["tax"] = money(bad * inv["tax_rate"])
        inv["total"] = money(bad + inv["tax"] + inv["freight"])
        inv["error_note"] = (
            "Printed subtotal is 9.00 higher than the sum of the printed line "
            "amounts (digit transposition). Tax and total are internally "
            "consistent with the wrong subtotal, so only the line-sum check "
            "catches this."
        )

    elif kind == "TOTAL_MISMATCH":
        # Freight was charged but omitted from the total
        inv["total"] = money(inv["subtotal"] + inv["tax"])
        if inv["freight"] == 0:
            inv["freight"] = money(random.uniform(40, 120))
        inv["error_note"] = (
            "Freight appears as a charge but was not added into the printed "
            "total. subtotal + tax + freight != total."
        )

    elif kind == "DUPLICATE_INVOICE_NO":
        twin = next(i for i in all_invoices
                    if i["vendor_name"] == inv["vendor_name"] and i is not inv)
        inv["invoice_number"] = twin["invoice_number"]
        inv["error_note"] = (
            f"Same vendor and invoice number as {twin['file_id']}, different "
            f"date and amount. This is what a re-sent or re-keyed invoice "
            f"looks like, and posting both double-pays the vendor."
        )

    elif kind == "UNKNOWN_VENDOR":
        v = dataset.ROGUE_VENDOR
        inv["vendor"] = v
        inv["vendor_name"] = v["name"]
        inv["line_items"] = make_line_items(v, len(inv["line_items"]))
        ls = money(sum(i["amount"] for i in inv["line_items"]))
        inv["line_item_sum"] = ls
        inv["subtotal"] = ls
        inv["tax_rate"] = Decimal(str(v["tax_rate"]))
        inv["tax"] = money(ls * inv["tax_rate"])
        inv["freight"] = money(0)
        inv["total"] = money(ls + inv["tax"])
        inv["terms"] = v["terms"]
        inv["invoice_number"] = f"PFS-{random.randint(10000, 99999)}"
        inv["error_note"] = (
            "Vendor does not appear in the vendor master list. Document is "
            "internally consistent — only a lookup against known vendors "
            "catches it."
        )

    elif kind == "STALE_DATE":
        old = TODAY - timedelta(days=random.randint(760, 900))
        inv["invoice_date"] = old
        inv["due_date"] = due_date(old, inv["terms"])
        inv["error_note"] = (
            "Invoice dated more than two years in the past. Either a very late "
            "submission or a keying error; either way it should not post "
            "silently into the current period."
        )

    elif kind == "FUTURE_DATE":
        fut = TODAY + timedelta(days=random.randint(45, 120))
        inv["invoice_date"] = fut
        inv["due_date"] = due_date(fut, inv["terms"])
        inv["error_note"] = (
            "Invoice dated in the future. Common symptom of a wrong year on "
            "the vendor's template."
        )

    elif kind == "MISSING_INVOICE_NO":
        inv["invoice_number"] = None
        inv["error_note"] = (
            "No invoice number printed anywhere on the document. Nothing to "
            "duplicate-check against, so it cannot be posted unattended."
        )

    else:
        raise ValueError(kind)

    return inv


# ---------------------------------------------------------------------------
# Layout A — Modern. Sans-serif, colour accent bar, zebra table, totals right.
# ---------------------------------------------------------------------------

def layout_modern(c, inv):
    W, H = letter
    v = inv["vendor"]
    m = 0.75 * inch

    c.setFillColorRGB(0.13, 0.31, 0.45)
    c.rect(0, H - 1.45 * inch, W, 1.45 * inch, fill=1, stroke=0)

    c.setFillColorRGB(1, 1, 1)
    c.setFont(DJSans_B, 19)
    c.drawString(m, H - 0.72 * inch, v["name"])
    c.setFont(DJSans, 8.5)
    c.drawString(m, H - 0.95 * inch, v["addr1"])
    c.drawString(m, H - 1.12 * inch, f"{v['city']}, {v['state']} {v['zip']}   ·   {v['phone']}")

    c.setFont(DJSans_B, 26)
    c.drawRightString(W - m, H - 0.80 * inch, "INVOICE")

    y = H - 1.45 * inch - 0.42 * inch
    c.setFillColorRGB(0.25, 0.25, 0.25)
    c.setFont(DJSans_B, 8)
    c.drawString(m, y, "BILL TO")
    c.setFont(DJSans, 9)
    b = dataset.BUYER
    for i, line in enumerate([b["name"], b["addr1"], b["addr2"],
                              f"{b['city']}, {b['state']} {b['zip']}"]):
        c.drawString(m, y - 14 - i * 12, line)

    fields = []
    if inv["invoice_number"]:
        fields.append(("Invoice No.", inv["invoice_number"]))
    fields.append(("Invoice Date", fmt_date(inv["invoice_date"], inv["date_format"])))
    fields.append(("Due Date", fmt_date(inv["due_date"], inv["date_format"])))
    fields.append(("Terms", inv["terms"]))
    if inv["po_number"]:
        fields.append(("PO Number", inv["po_number"]))

    fy = y
    for label, val in fields:
        c.setFont(DJSans, 8)
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.drawRightString(W - m - 1.35 * inch, fy, label)
        c.setFont(DJSans_B, 9)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawRightString(W - m, fy, str(val))
        fy -= 15

    ty = min(y - 14 - 4 * 12, fy) - 30
    cols = [m, m + 3.55 * inch, m + 4.25 * inch, m + 5.30 * inch, W - m]

    c.setFillColorRGB(0.13, 0.31, 0.45)
    c.rect(m, ty - 4, W - 2 * m, 20, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(DJSans_B, 8.5)
    c.drawString(cols[0] + 6, ty + 2, "DESCRIPTION")
    c.drawRightString(cols[2] - 8, ty + 2, "QTY")
    c.drawRightString(cols[3] - 8, ty + 2, "UNIT PRICE")
    c.drawRightString(cols[4] - 6, ty + 2, "AMOUNT")

    ry = ty - 20
    for n, it in enumerate(inv["line_items"]):
        if n % 2 == 0:
            c.setFillColorRGB(0.96, 0.97, 0.98)
            c.rect(m, ry - 4, W - 2 * m, 17, fill=1, stroke=0)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.setFont(DJSans, 8.5)
        c.drawString(cols[0] + 6, ry, it["description"][:58])
        c.drawRightString(cols[2] - 8, ry, f"{it['quantity']} {it['unit']}")
        c.drawRightString(cols[3] - 8, ry, fmt(it["unit_price"]))
        c.drawRightString(cols[4] - 6, ry, fmt(it["amount"]))
        ry -= 17

    ry -= 12
    rows = [("Subtotal", inv["subtotal"])]
    if inv["tax_rate"] > 0:
        rows.append((f"Sales Tax ({inv['tax_rate'] * 100:.2f}%)", inv["tax"]))
    if inv["freight"] > 0:
        rows.append(("Freight", inv["freight"]))

    for label, val in rows:
        c.setFont(DJSans, 9)
        c.drawRightString(W - m - 1.15 * inch, ry, label)
        c.drawRightString(W - m, ry, fmt(val))
        ry -= 15

    c.setStrokeColorRGB(0.13, 0.31, 0.45)
    c.setLineWidth(1.2)
    c.line(W - m - 2.6 * inch, ry + 8, W - m, ry + 8)
    ry -= 6
    c.setFont(DJSans_B, 12)
    c.setFillColorRGB(0.13, 0.31, 0.45)
    c.drawRightString(W - m - 1.15 * inch, ry, "TOTAL DUE")
    c.drawRightString(W - m, ry, fmt(inv["total"], symbol=True))

    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.setFont(DJSans, 7.5)
    c.drawString(m, 0.6 * inch, f"Remit to {v['name']}, {v['addr1']}, {v['city']}, {v['state']} {v['zip']}")
    c.drawRightString(W - m, 0.6 * inch, "Thank you for your business.")


# ---------------------------------------------------------------------------
# Layout B — Classic. Serif, ruled box header, tax folded into a footer block.
# ---------------------------------------------------------------------------

def layout_classic(c, inv):
    W, H = letter
    v = inv["vendor"]
    m = 0.9 * inch

    c.setFillColorRGB(0, 0, 0)
    c.setFont(DJSerif_B, 16)
    c.drawCentredString(W / 2, H - 0.85 * inch, v["name"].upper())
    c.setFont(DJSerif, 9)
    c.drawCentredString(W / 2, H - 1.05 * inch, f"{v['addr1']}  ·  {v['city']}, {v['state']} {v['zip']}")
    c.drawCentredString(W / 2, H - 1.20 * inch, f"Telephone {v['phone']}")

    c.setLineWidth(1.6)
    c.line(m, H - 1.35 * inch, W - m, H - 1.35 * inch)
    c.setLineWidth(0.5)
    c.line(m, H - 1.39 * inch, W - m, H - 1.39 * inch)

    c.setFont(DJSerif_B, 12)
    c.drawCentredString(W / 2, H - 1.68 * inch, "I N V O I C E")

    boxtop = H - 1.90 * inch
    boxh = 0.95 * inch
    c.setLineWidth(0.8)
    c.rect(m, boxtop - boxh, W - 2 * m, boxh, fill=0, stroke=1)
    c.line(W / 2, boxtop - boxh, W / 2, boxtop)

    c.setFont(DJSerif_B, 8)
    c.drawString(m + 8, boxtop - 14, "SOLD TO")
    c.setFont(DJSerif, 9)
    b = dataset.BUYER
    for i, line in enumerate([b["name"], b["addr1"], b["addr2"],
                              f"{b['city']}, {b['state']} {b['zip']}"]):
        c.drawString(m + 8, boxtop - 28 - i * 12, line)

    rx = W / 2 + 10
    ry = boxtop - 14
    pairs = []
    if inv["invoice_number"]:
        pairs.append(("Invoice Number", inv["invoice_number"]))
    pairs.append(("Date", fmt_date(inv["invoice_date"], inv["date_format"])))
    pairs.append(("Terms", inv["terms"]))
    pairs.append(("Due", fmt_date(inv["due_date"], inv["date_format"])))
    if inv["po_number"]:
        pairs.append(("Customer P.O.", inv["po_number"]))
    for label, val in pairs:
        c.setFont(DJSerif, 8.5)
        c.drawString(rx, ry, f"{label}:")
        c.setFont(DJSerif_B, 8.5)
        c.drawRightString(W - m - 8, ry, str(val))
        ry -= 12.5

    ty = boxtop - boxh - 0.35 * inch
    c.setFont(DJSerif_B, 8.5)
    c.drawString(m, ty, "QUANTITY")
    c.drawString(m + 0.95 * inch, ty, "DESCRIPTION")
    c.drawRightString(W - m - 1.25 * inch, ty, "PRICE")
    c.drawRightString(W - m, ty, "AMOUNT")
    c.setLineWidth(0.8)
    c.line(m, ty - 5, W - m, ty - 5)

    ry = ty - 20
    for it in inv["line_items"]:
        c.setFont(DJSerif, 9)
        c.drawString(m, ry, f"{it['quantity']} {it['unit']}")
        c.drawString(m + 0.95 * inch, ry, it["description"][:52])
        c.drawRightString(W - m - 1.25 * inch, ry, fmt(it["unit_price"]))
        c.drawRightString(W - m, ry, fmt(it["amount"]))
        ry -= 15

    c.setLineWidth(0.5)
    c.line(W - m - 2.9 * inch, ry + 4, W - m, ry + 4)
    ry -= 12

    c.setFont(DJSerif, 9)
    c.drawRightString(W - m - 1.25 * inch, ry, "Subtotal")
    c.drawRightString(W - m, ry, fmt(inv["subtotal"]))
    ry -= 14
    if inv["freight"] > 0:
        c.drawRightString(W - m - 1.25 * inch, ry, "Freight & Handling")
        c.drawRightString(W - m, ry, fmt(inv["freight"]))
        ry -= 14
    if inv["tax_rate"] > 0:
        c.drawRightString(W - m - 1.25 * inch, ry, f"Sales Tax @ {inv['tax_rate'] * 100:.2f}%")
        c.drawRightString(W - m, ry, fmt(inv["tax"]))
        ry -= 14

    c.setLineWidth(1.2)
    c.line(W - m - 2.9 * inch, ry + 6, W - m, ry + 6)
    ry -= 8
    c.setFont(DJSerif_B, 11)
    c.drawRightString(W - m - 1.25 * inch, ry, "TOTAL")
    c.drawRightString(W - m, ry, fmt(inv["total"], symbol=True))

    c.setFont(DJSerif, 8)
    c.drawCentredString(W / 2, 0.72 * inch,
                        "Please remit payment per terms above. A 1.5% monthly service charge applies to past due balances.")


# ---------------------------------------------------------------------------
# Layout C — Dense. Condensed font, many line items, spills onto page 2.
# ---------------------------------------------------------------------------

def layout_dense(c, inv):
    W, H = letter
    v = inv["vendor"]
    m = 0.55 * inch

    def header(page_no, total_pages):
        c.setFillColorRGB(0, 0, 0)
        c.setFont(DJCond_B, 13)
        c.drawString(m, H - 0.52 * inch, v["name"])
        c.setFont(DJCond, 7.5)
        c.drawString(m, H - 0.68 * inch,
                     f"{v['addr1']}, {v['city']}, {v['state']} {v['zip']}  |  {v['phone']}")
        c.setFont(DJCond_B, 11)
        c.drawRightString(W - m, H - 0.52 * inch, "INVOICE")
        c.setFont(DJCond, 7.5)
        c.drawRightString(W - m, H - 0.68 * inch, f"Page {page_no} of {total_pages}")

        yy = H - 0.92 * inch
        b = dataset.BUYER
        c.setFont(DJCond_B, 7.5)
        c.drawString(m, yy, "BILL TO:")
        c.setFont(DJCond, 8)
        c.drawString(m + 0.55 * inch, yy, f"{b['name']}, {b['addr1']} {b['addr2']}, {b['city']}, {b['state']} {b['zip']}")

        yy -= 13
        bits = []
        if inv["invoice_number"]:
            bits.append(f"INV {inv['invoice_number']}")
        bits.append(f"DATE {fmt_date(inv['invoice_date'], inv['date_format'])}")
        bits.append(f"TERMS {inv['terms']}")
        bits.append(f"DUE {fmt_date(inv['due_date'], inv['date_format'])}")
        if inv["po_number"]:
            bits.append(f"CUST PO {inv['po_number']}")
        c.setFont(DJCond, 8)
        c.drawString(m, yy, "   |   ".join(bits))

        yy -= 16
        c.setFillColorRGB(0.88, 0.88, 0.88)
        c.rect(m, yy - 3, W - 2 * m, 13, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.setFont(DJCond_B, 7.5)
        c.drawString(m + 3, yy, "LINE")
        c.drawString(m + 0.42 * inch, yy, "DESCRIPTION")
        c.drawRightString(m + 5.05 * inch, yy, "QTY")
        c.drawRightString(m + 5.55 * inch, yy, "UM")
        c.drawRightString(m + 6.35 * inch, yy, "UNIT PRICE")
        c.drawRightString(W - m - 3, yy, "EXTENDED")
        return yy - 14

    per_page = 26
    items = inv["line_items"]
    pages = max(1, (len(items) + per_page - 1) // per_page)

    idx = 0
    for p in range(1, pages + 1):
        ry = header(p, pages)
        chunk = items[idx:idx + per_page]
        for it in chunk:
            idx += 1
            c.setFont(DJCond, 7.8)
            c.drawString(m + 3, ry, f"{idx:03d}")
            c.drawString(m + 0.42 * inch, ry, it["description"][:64])
            c.drawRightString(m + 5.05 * inch, ry, str(it["quantity"]))
            c.drawRightString(m + 5.55 * inch, ry, it["unit"])
            c.drawRightString(m + 6.35 * inch, ry, fmt(it["unit_price"]))
            c.drawRightString(W - m - 3, ry, fmt(it["amount"]))
            ry -= 11

        if p < pages:
            c.setFont(DJCond, 8)
            c.drawRightString(W - m - 3, ry - 8, "continued ...")
            c.showPage()

    ry -= 10
    c.setLineWidth(0.6)
    c.line(W - m - 2.4 * inch, ry + 6, W - m, ry + 6)
    ry -= 6
    rows = [("SUBTOTAL", inv["subtotal"])]
    if inv["freight"] > 0:
        rows.append(("FREIGHT", inv["freight"]))
    if inv["tax_rate"] > 0:
        rows.append((f"TAX {inv['tax_rate'] * 100:.2f}%", inv["tax"]))
    for label, val in rows:
        c.setFont(DJCond, 8.5)
        c.drawRightString(W - m - 1.05 * inch, ry, label)
        c.drawRightString(W - m - 3, ry, fmt(val))
        ry -= 12
    c.setFont(DJCond_B, 10)
    c.drawRightString(W - m - 1.05 * inch, ry - 2, "TOTAL DUE")
    c.drawRightString(W - m - 3, ry - 2, fmt(inv["total"], symbol=True))


# ---------------------------------------------------------------------------
# Layout D — Minimal. No rules, generous whitespace, tax as a line item.
# ---------------------------------------------------------------------------

def layout_minimal(c, inv):
    W, H = letter
    v = inv["vendor"]
    m = 1.1 * inch

    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.setFont(LibSans_B, 14)
    c.drawString(m, H - 1.2 * inch, v["name"])
    c.setFont(LibSans, 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(m, H - 1.40 * inch, f"{v['addr1']}, {v['city']}, {v['state']} {v['zip']}")

    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.setFont(LibSans, 9)
    y = H - 2.1 * inch
    lines = []
    if inv["invoice_number"]:
        lines.append(f"Invoice {inv['invoice_number']}")
    lines.append(f"Issued {fmt_date(inv['invoice_date'], inv['date_format'])}")
    lines.append(f"Due {fmt_date(inv['due_date'], inv['date_format'])} · {inv['terms']}")
    if inv["po_number"]:
        lines.append(f"Reference {inv['po_number']}")
    for ln in lines:
        c.drawString(m, y, ln)
        y -= 14

    y -= 10
    b = dataset.BUYER
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(m, y, "For")
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(m + 0.45 * inch, y, f"{b['name']} · {b['city']}, {b['state']}")

    y -= 36
    for it in inv["line_items"]:
        c.setFont(LibSans, 9.5)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(m, y, it["description"][:56])
        c.setFillColorRGB(0.45, 0.45, 0.45)
        c.setFont(LibSans, 8.5)
        c.drawString(m, y - 11, f"{it['quantity']} {it['unit']} @ {fmt(it['unit_price'])}")
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.setFont(LibSans, 9.5)
        c.drawRightString(W - m, y, fmt(it["amount"]))
        y -= 28

    # Subtotal, then tax and freight presented as further line items
    y -= 6
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(0.7)
    c.line(m, y + 10, W - m, y + 10)

    c.setFont(LibSans, 9.5)
    c.drawString(m, y, "Subtotal")
    c.drawRightString(W - m, y, fmt(inv["subtotal"]))
    y -= 18
    if inv["freight"] > 0:
        c.drawString(m, y, "Shipping")
        c.drawRightString(W - m, y, fmt(inv["freight"]))
        y -= 18
    if inv["tax_rate"] > 0:
        c.drawString(m, y, f"Sales tax ({inv['tax_rate'] * 100:.2f}%)")
        c.drawRightString(W - m, y, fmt(inv["tax"]))
        y -= 18

    c.line(m, y + 10, W - m, y + 10)
    y -= 4
    c.setFont(LibSans_B, 11)
    c.drawString(m, y, "Amount due")
    c.drawRightString(W - m, y, fmt(inv["total"], symbol=True))

    c.setFont(LibSans, 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(m, 0.9 * inch, f"Questions? {v['phone']}")


# ---------------------------------------------------------------------------
# Layout E — Scanned. Rendered as a raster, then degraded: skew, blur,
# speckle, uneven exposure. This is the one that separates a real pipeline
# from a demo that only ever saw clean digital PDFs.
# ---------------------------------------------------------------------------

def layout_scanned(c, inv):
    W, H = letter
    dpi = 150
    px_w, px_h = int(8.5 * dpi), int(11 * dpi)

    img = Image.new("L", (px_w, px_h), 255)
    d = ImageDraw.Draw(img)

    def F(name, size):
        role = {"r": "sans", "b": "sans-bold", "m": "mono"}[name]
        path = fonts.truetype_path(role)
        if path is None:
            # PIL has no built-in scalable font, so a machine with no usable
            # TTF gets a fixed-size bitmap and a cosmetically poorer scan.
            # Generation still completes, which is what matters.
            return ImageFont.load_default()
        return ImageFont.truetype(path, size)

    v = inv["vendor"]
    M = int(0.8 * dpi)

    d.text((M, int(0.55 * dpi)), v["name"], font=F("b", 30), fill=30)
    d.text((M, int(0.83 * dpi)), v["addr1"], font=F("r", 16), fill=60)
    d.text((M, int(1.00 * dpi)), f"{v['city']}, {v['state']} {v['zip']}", font=F("r", 16), fill=60)
    d.text((M, int(1.17 * dpi)), v["phone"], font=F("r", 16), fill=60)

    d.text((px_w - M - 200, int(0.58 * dpi)), "INVOICE", font=F("b", 34), fill=30)

    y = int(1.55 * dpi)
    d.line([(M, y), (px_w - M, y)], fill=90, width=3)

    y += 20
    b = dataset.BUYER
    d.text((M, y), "BILL TO:", font=F("b", 15), fill=40)
    for i, line in enumerate([b["name"], b["addr1"], b["addr2"],
                              f"{b['city']}, {b['state']} {b['zip']}"]):
        d.text((M, y + 24 + i * 21), line, font=F("r", 16), fill=45)

    rx = px_w - M - 380
    ry = y
    rows = []
    if inv["invoice_number"]:
        rows.append(("INVOICE NO", inv["invoice_number"]))
    rows.append(("DATE", fmt_date(inv["invoice_date"], inv["date_format"])))
    rows.append(("TERMS", inv["terms"]))
    rows.append(("DUE DATE", fmt_date(inv["due_date"], inv["date_format"])))
    if inv["po_number"]:
        rows.append(("PO", inv["po_number"]))
    for label, val in rows:
        d.text((rx, ry), label, font=F("b", 13), fill=55)
        d.text((rx + 170, ry), str(val), font=F("r", 15), fill=35)
        ry += 22

    y = max(y + 24 + 4 * 21, ry) + 34
    d.rectangle([M, y, px_w - M, y + 28], fill=225)
    d.text((M + 8, y + 6), "DESCRIPTION", font=F("b", 14), fill=35)
    d.text((px_w - M - 470, y + 6), "QTY", font=F("b", 14), fill=35)
    d.text((px_w - M - 350, y + 6), "PRICE", font=F("b", 14), fill=35)
    d.text((px_w - M - 165, y + 6), "AMOUNT", font=F("b", 14), fill=35)

    y += 38
    for it in inv["line_items"]:
        d.text((M + 8, y), it["description"][:50], font=F("r", 15), fill=40)
        d.text((px_w - M - 470, y), f"{it['quantity']} {it['unit']}", font=F("r", 15), fill=40)
        d.text((px_w - M - 350, y), fmt(it["unit_price"]), font=F("r", 15), fill=40)
        d.text((px_w - M - 165, y), fmt(it["amount"]), font=F("r", 15), fill=40)
        y += 25

    y += 16
    d.line([(px_w - M - 420, y), (px_w - M, y)], fill=110, width=2)
    y += 12
    tot_rows = [("SUBTOTAL", inv["subtotal"])]
    if inv["freight"] > 0:
        tot_rows.append(("FREIGHT", inv["freight"]))
    if inv["tax_rate"] > 0:
        tot_rows.append((f"TAX {inv['tax_rate'] * 100:.2f}%", inv["tax"]))
    for label, val in tot_rows:
        d.text((px_w - M - 420, y), label, font=F("r", 15), fill=40)
        d.text((px_w - M - 165, y), fmt(val), font=F("r", 15), fill=40)
        y += 24
    d.line([(px_w - M - 420, y), (px_w - M, y)], fill=110, width=3)
    y += 10
    d.text((px_w - M - 420, y), "TOTAL DUE", font=F("b", 19), fill=25)
    d.text((px_w - M - 175, y), fmt(inv["total"], symbol=True), font=F("b", 19), fill=25)

    # ---- degrade -----------------------------------------------------------
    # The point of this layout is that it has no text layer and does not read
    # cleanly. If it comes out looking like a crisp digital PDF, it isn't
    # testing anything the other four layouts don't already cover.

    # Re-sample down and back up: the single biggest contributor to that
    # soft, slightly mushy scanned-document look.
    small = img.resize((int(px_w * 0.62), int(px_h * 0.62)), Image.BILINEAR)
    img = small.resize((px_w, px_h), Image.BILINEAR)

    img = img.rotate(random.uniform(-1.5, 1.5), resample=Image.BICUBIC,
                     fillcolor=246, expand=False)
    img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.9, 1.4)))

    px = img.load()
    # Speckle and dropout: dark flecks from dust on the glass, light pinholes
    # where toner didn't take.
    for _ in range(int(px_w * px_h * 0.0045)):
        x = random.randrange(px_w)
        yy = random.randrange(px_h)
        px[x, yy] = random.randint(40, 150)
    for _ in range(int(px_w * px_h * 0.0016)):
        x = random.randrange(px_w)
        yy = random.randrange(px_h)
        if px[x, yy] < 160:
            px[x, yy] = random.randint(180, 240)

    # A few horizontal scanner streaks
    for _ in range(random.randint(2, 5)):
        yy = random.randrange(int(px_h * 0.1), int(px_h * 0.95))
        shade = random.randint(198, 232)
        d2 = ImageDraw.Draw(img)
        d2.line([(random.randrange(0, px_w // 3), yy),
                 (random.randrange(px_w // 2, px_w), yy)],
                fill=shade, width=random.choice([1, 1, 2]))

    # Uneven exposure, as if the page lifted off the glass along one edge
    grad = Image.new("L", (px_w, px_h), 255)
    gd = ImageDraw.Draw(grad)
    edge = random.choice(["left", "right"])
    for i in range(0, px_w, 4):
        frac = i / px_w if edge == "right" else 1 - (i / px_w)
        shade = int(255 - 58 * (frac ** 2.0))
        gd.rectangle([i, 0, i + 4, px_h], fill=shade)
    mask = img.point(lambda p: 255 if p < 200 else 0)
    img = Image.blend(img, Image.composite(img, grad, mask), 0.85)

    # Grey the paper down and lift the blacks — scanned pages are never
    # white-on-black, they are grey-on-grey.
    img = img.point(lambda p: max(0, min(255, int(30 + p * 0.86))))
    img = img.filter(ImageFilter.GaussianBlur(radius=0.35))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    c.drawImage(ImageReader(buf), 0, 0, width=W, height=H)


LAYOUTS = {
    "modern": layout_modern,
    "classic": layout_classic,
    "dense": layout_dense,
    "minimal": layout_minimal,
    "scanned": layout_scanned,
}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def serialise(inv):
    """Ground truth for one document: exactly what a careful human would
    transcribe from the page."""
    return {
        "file": f"{inv['file_id']}.pdf",
        "layout": inv["layout"],
        "vendor_name": inv["vendor_name"],
        "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"].isoformat(),
        "due_date": inv["due_date"].isoformat(),
        "date_format_on_document": inv["date_format"],
        "terms": inv["terms"],
        "po_number": inv["po_number"],
        "currency": inv["currency"],
        "line_items": [
            {
                "description": i["description"],
                "quantity": i["quantity"],
                "unit": i["unit"],
                "unit_price": str(i["unit_price"]),
                "amount": str(i["amount"]),
            }
            for i in inv["line_items"]
        ],
        "line_item_count": len(inv["line_items"]),
        "sum_of_line_amounts": str(inv["line_item_sum"]),
        "subtotal_printed": str(inv["subtotal"]),
        "tax_rate": str(inv["tax_rate"]),
        "tax_printed": str(inv["tax"]),
        "freight_printed": str(inv["freight"]),
        "total_printed": str(inv["total"]),
        "seeded_error": inv["seeded_error"],
        "error_note": inv["error_note"],
        "expected_disposition": "REVIEW" if inv["seeded_error"] else "POST",
    }


def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    plan = [
        # (layout, n_items) — 24 documents, weighted toward the common shapes
        ("modern", 6), ("classic", 5), ("dense", 34), ("minimal", 4),
        ("modern", 8), ("scanned", 6), ("classic", 7), ("modern", 5),
        ("minimal", 3), ("dense", 29), ("modern", 9), ("classic", 4),
        ("scanned", 5), ("modern", 6), ("minimal", 5), ("classic", 6),
        ("dense", 41), ("modern", 7), ("scanned", 4), ("classic", 5),
        ("minimal", 6), ("modern", 4), ("dense", 22), ("classic", 8),
    ]

    invoices = []
    seq = 4820
    for i, (layout, n) in enumerate(plan, start=1):
        vendor = dataset.VENDORS[(i * 5) % len(dataset.VENDORS)]
        issued = TODAY - timedelta(days=random.randint(2, 75))
        seq += random.randint(3, 41)
        invoices.append(build_invoice(i, vendor, layout, n, issued, seq))

    # Seed one defect each into seven documents, spread across layouts.
    # DUPLICATE_INVOICE_NO needs a same-vendor twin, so it is handled first
    # against the already-built set.
    targets = {
        "DUPLICATE_INVOICE_NO": 14,   # vendor repeats earlier in the set
        "LINE_SUM_MISMATCH": 2,
        "TOTAL_MISMATCH": 17,
        "UNKNOWN_VENDOR": 9,
        "STALE_DATE": 6,
        "FUTURE_DATE": 20,
        "MISSING_INVOICE_NO": 11,
    }
    for kind, idx in targets.items():
        inv = invoices[idx - 1]
        if kind == "DUPLICATE_INVOICE_NO":
            same = [x for x in invoices
                    if x["vendor_name"] == inv["vendor_name"] and x is not inv]
            if not same:
                inv["vendor"] = invoices[0]["vendor"]
                inv["vendor_name"] = invoices[0]["vendor_name"]
                inv["line_items"] = make_line_items(inv["vendor"], len(inv["line_items"]))
                ls = money(sum(x["amount"] for x in inv["line_items"]))
                inv["line_item_sum"] = ls
                inv["subtotal"] = ls
                inv["tax_rate"] = Decimal(str(inv["vendor"]["tax_rate"]))
                inv["tax"] = money(ls * inv["tax_rate"])
                inv["total"] = money(ls + inv["tax"] + inv["freight"])
        seed_error(inv, kind, invoices)

    for inv in invoices:
        # invariant=1 strips the embedded creation timestamp and document ID.
        # Without it, regenerating an unchanged set still rewrites all 24 PDFs
        # at the byte level, so `git status` reports modifications that aren't.
        c = rl_canvas.Canvas(str(PDF_DIR / f"{inv['file_id']}.pdf"),
                             pagesize=letter, invariant=1)
        c.setTitle(f"Invoice {inv['invoice_number'] or '(no number)'}")
        c.setAuthor(inv["vendor_name"])
        c.setSubject("Fictitious invoice — synthetic test data")
        LAYOUTS[inv["layout"]](c, inv)
        c.showPage()
        c.save()

    manifest = {
        "generated_for": "Invoice extraction + validation pipeline demo",
        "buyer": dataset.BUYER["name"],
        "generated_on": TODAY.isoformat(),
        "random_seed": SEED,
        "document_count": len(invoices),
        "clean_count": sum(1 for i in invoices if not i["seeded_error"]),
        "seeded_count": sum(1 for i in invoices if i["seeded_error"]),
        "notice": (
            "Every company, person, address, phone number and transaction in "
            "this set is invented. Nothing here corresponds to a real vendor, "
            "customer or employer."
        ),
        "manifest_semantics": (
            "Fields ending in _printed record what appears on the page, not "
            "what would have been arithmetically correct. Extraction accuracy "
            "is measured against the printed values. Whether those values "
            "reconcile is what the validation layer decides."
        ),
        "known_vendors": [v["name"] for v in dataset.VENDORS],
        "documents": [serialise(i) for i in invoices],
    }

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       newline="\n", encoding="utf-8")

    with open(OUT / "vendors.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Vendor Name", "Address", "City", "State", "Zip", "Phone",
                    "Terms", "Tax Rate", "Default Expense Account"])
        for v in dataset.VENDORS:
            w.writerow([v["name"], v["addr1"], v["city"], v["state"], v["zip"],
                        v["phone"], v["terms"], f"{v['tax_rate']:.4f}", v["account"]])

    print(f"{len(invoices)} PDFs -> {PDF_DIR}")
    for i in invoices:
        flag = f"  <-- {i['seeded_error']}" if i["seeded_error"] else ""
        print(f"  {i['file_id']}  {i['layout']:8s}  {i['vendor_name'][:34]:34s} "
              f"{fmt(i['total'], symbol=True):>12s}{flag}")


if __name__ == "__main__":
    main()
