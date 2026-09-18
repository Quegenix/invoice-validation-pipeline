#!/usr/bin/env python3
"""
What the system has learned about each vendor, and what it does with it.

The alternative design was a wizard: show a hard invoice, have someone
highlight where each field lives, save the rectangles. That is how the
template products work, and it fails for three reasons here.

  It solves a problem we do not have. Across 235 documents the extractor
  found the right field every single time. What it got wrong was the
  character -- 3 for 1, O for 0, S for 5. A zone map says where to look, not
  what the glyph is.

  Rectangles assume the page is registered the same way twice, and the entire
  failure population is the fax/scan stream: skewed, rotated, variable
  margins. The zone would be least reliable exactly where it is most needed.

  And "teach it once" is a promise a crooked scan breaks. A box that silently
  starts pointing at the wrong thing is worse than no box, because a zone
  always contains something.

So the same goal -- per-vendor knowledge that makes future scans better --
is met by learning from history instead. The ledger already records every
document; a vendor's habits fall out of it for free, with nothing for anyone
to set up and nothing to break when a vendor redesigns their stationery.

Two rules come from this, and both were measured before being written:

  identifier shape   a vendor's invoice numbers follow a house pattern, so a
                     read that breaks it is probably a misread.
  identifier range   invoice numbers march forward. A number far below
                     everything seen from that vendor is probably a misread.

Measured chronologically -- learning only from invoices DATED EARLIER, which
is all a ledger can ever know -- these caught 4 of the 5 testable misreads
with no false positives across 71 correctly-read numbers. The nine that were
not testable belonged to vendors with fewer than two prior invoices, which is
what a one-month snapshot looks like and not what three months of use looks
like.

READ THAT RESULT NARROWLY. The test set generates every invoice number as
<vendor initials><6-digit sequence>, so a vendor's shape is constant BY
CONSTRUCTION and the sequence rises monotonically. Both checks therefore had
an easier target than real invoices will give them: real numbering carries
year prefixes that roll over, branch codes, variable-length sequences, and
whole formats that change when a vendor switches systems. The false-positive
rate here is evidence that the mechanism is not obviously broken, and nothing
more. It needs measuring against a client'"'"'s own history before it is quoted
to anyone.

What survives that caveat is the failure DIRECTION. Shapes are learned as a
set and nothing is claimed below MIN_PRIORS invoices, so a vendor with three
numbering formats produces no signal rather than three false alarms. The
check degrades into silence, which is the only acceptable way for it to be
wrong.

An earlier measurement that learned from each vendor's *future* invoices too
produced 11 false positives in 121. That difference is the whole lesson:
a profile must only ever know the past.

The wizard is not gone, it is scoped. Some things genuinely cannot be
derived -- which of two printed totals to post on a retainage invoice, which
of three dates is the invoice date. Those are stored as a HINT: a sentence,
not a rectangle, injected into the extraction prompt for that vendor.
A sentence survives a redesign and a two-degree rotation. A rectangle does not.
"""

import html
import json
import re
from collections import Counter
from datetime import date

MIN_PRIORS = 2      # below this a vendor has no profile and nothing is claimed
DROP_FRACTION = 0.20  # how far below the lowest seen number counts as wrong
AMOUNT_FACTOR = 4.0   # order-of-magnitude guard on totals


def shape(s):
    """Letters -> A, digits -> 9, everything else literal."""
    return "".join("9" if c.isdigit() else "A" if c.isalpha() else c
                   for c in (s or ""))


def numpart(s):
    d = re.findall(r"\d+", s or "")
    return int(max(d, key=len)) if d else None


def _f(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def build(ledger, client):
    """
    Derive a profile per vendor from everything the ledger has seen.

    Only documents that were imported contribute. A held or rejected document
    is one nobody vouched for, and learning a vendor's habits from records
    that were never accepted would teach the profile to expect mistakes.
    """
    imported = ledger._imported_ids(client)
    rows = ledger.db.execute(
        "SELECT doc_id,vendor,invoice_number,invoice_date,total,currency,"
        "extracted,batch_id,seen_at FROM documents WHERE client=? ORDER BY invoice_date",
        (client,)).fetchall()

    edits = Counter()
    for r in ledger.db.execute(
            "SELECT doc_id FROM events WHERE action='edited'"):
        edits[r["doc_id"]] += 1

    by = {}
    for r in rows:
        v = (r["vendor"] or "").strip()
        if not v or r["doc_id"] not in imported:
            continue
        p = by.setdefault(v, {
            "vendor": v, "count": 0, "numbers": [], "shapes": Counter(),
            "nums": [], "dates": [], "totals": [], "tax_rates": Counter(),
            "freight": Counter(), "currency": Counter(), "corrections": 0,
            "batches": set(),
        })
        p["count"] += 1
        p["batches"].add(r["batch_id"])
        p["corrections"] += edits.get(r["doc_id"], 0)
        n = (r["invoice_number"] or "").strip()
        if n:
            p["numbers"].append(n)
            p["shapes"][shape(n)] += 1
            if numpart(n) is not None:
                p["nums"].append(numpart(n))
        if r["invoice_date"]:
            p["dates"].append(r["invoice_date"])
        t = _f(r["total"])
        if t is not None:
            p["totals"].append(t)
        p["currency"][r["currency"] or "USD"] += 1
        try:
            ex = json.loads(r["extracted"])
        except (TypeError, ValueError):
            ex = {}
        sub, tax = _f(ex.get("subtotal")), _f(ex.get("tax"))
        if sub:
            p["tax_rates"][round((tax or 0) / sub, 4)] += 1
        p["freight"]["yes" if _f(ex.get("freight")) else "no"] += 1

    hints = {r["vendor"]: r["hint"] for r in ledger.db.execute(
        "SELECT vendor,hint FROM vendor_hints WHERE client=?", (client,))}

    out = {}
    for v, p in by.items():
        p["batches"] = len(p["batches"])
        p["established"] = p["count"] >= MIN_PRIORS
        p["number_shapes"] = sorted(p["shapes"])
        p["number_lo"] = min(p["nums"]) if p["nums"] else None
        p["number_hi"] = max(p["nums"]) if p["nums"] else None
        p["first_seen"] = min(p["dates"]) if p["dates"] else None
        p["last_seen"] = max(p["dates"]) if p["dates"] else None
        p["total_lo"] = min(p["totals"]) if p["totals"] else None
        p["total_hi"] = max(p["totals"]) if p["totals"] else None
        p["tax_rate"] = (p["tax_rates"].most_common(1)[0][0]
                         if p["tax_rates"] else None)
        p["tax_stable"] = len(p["tax_rates"]) == 1 and p["count"] >= MIN_PRIORS
        p["charges_freight"] = (p["freight"].most_common(1)[0][0]
                                if p["freight"] else None)
        p["currencies"] = sorted(p["currency"])
        p["hint"] = hints.get(v, "")
        for k in ("shapes", "freight", "currency"):
            p.pop(k)
        p["tax_rates"] = dict(p["tax_rates"])
        out[v] = p
    return out


def check(extracted, profile):
    """
    Findings from what this vendor usually does. Returns [] when there is no
    established profile -- a first invoice from a new vendor is not evidence
    of anything, and claiming otherwise would flag every new relationship.
    """
    if not profile or not profile.get("established"):
        return []
    F = []
    inv = (extracted.get("invoice_number") or "").strip()

    if inv and profile["number_shapes"]:
        if shape(inv) not in profile["number_shapes"]:
            F.append({
                "code": "IDENTIFIER_FORMAT", "severity": "BLOCK",
                "message": "Invoice number does not match this vendor's format",
                "detail": (f"read as '{inv}' ({shape(inv)}); the last "
                           f"{profile['count']} from {profile['vendor']} all look "
                           f"like {', '.join(profile['number_shapes'])}. A letter "
                           f"where a digit belongs is usually a misread, and a "
                           f"misread invoice number is what defeats duplicate "
                           f"checking."),
            })

    v = numpart(inv)
    lo = profile["number_lo"]
    if v is not None and lo is not None and v < lo * (1 - DROP_FRACTION):
        F.append({
            "code": "IDENTIFIER_RANGE", "severity": "BLOCK",
            "message": "Invoice number is far below anything seen from this vendor",
            "detail": (f"read as '{inv}'; previous numbers from "
                       f"{profile['vendor']} run {profile['number_lo']}–"
                       f"{profile['number_hi']}. Invoice numbers go forwards, so "
                       f"this is more likely a misread digit than a real number."),
        })

    sub, tax = _f(extracted.get("subtotal")), _f(extracted.get("tax"))
    if profile["tax_stable"] and sub:
        seen = profile["tax_rate"]
        got = round((tax or 0) / sub, 4)
        if abs(got - seen) > 0.0025:
            F.append({
                "code": "TAX_RATE_UNUSUAL", "severity": "WARN",
                "message": "Tax rate differs from this vendor's usual rate",
                "detail": (f"this invoice is taxed at {got*100:.2f}%; the last "
                           f"{profile['count']} from {profile['vendor']} were all "
                           f"at {seen*100:.2f}%"),
            })

    cur = extracted.get("currency") or "USD"
    if profile["currencies"] and cur not in profile["currencies"]:
        F.append({
            "code": "CURRENCY_CHANGE", "severity": "BLOCK",
            "message": "Priced in a currency this vendor has not used before",
            "detail": (f"this invoice is in {cur}; every previous invoice from "
                       f"{profile['vendor']} was in "
                       f"{', '.join(profile['currencies'])}. Posting it as USD "
                       f"would enter the wrong amount."),
        })

    tot = _f(extracted.get("total"))
    hi = profile["total_hi"]
    if tot and hi and tot > hi * AMOUNT_FACTOR:
        F.append({
            "code": "AMOUNT_UNUSUAL", "severity": "WARN",
            "message": "Total is far larger than anything seen from this vendor",
            "detail": (f"{tot:,.2f}; previous invoices from {profile['vendor']} "
                       f"ran {profile['total_lo']:,.2f}–{hi:,.2f}"),
        })
    return F


# ---------------------------------------------------------------------------
# The page. This is the part a client actually sees, and the reason it exists
# is that a learned profile is invisible. "The system builds a picture of each
# vendor" is a sentence; this is a thing to look at.
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vendor profiles — __CLIENT__</title><style>
:root{--bg:#14161a;--panel:#1b1e24;--panel2:#222630;--line:#2e3440;
 --ink:#e6e9ef;--muted:#9aa3b2;--accent:#6ea8fe;--ok:#2f7f5b;--warn:#7a5a1e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 5px;font-size:17px;font-weight:600}
.sub{color:var(--muted);font-size:12.5px;max-width:78ch}
.grid{display:grid;gap:14px;padding:18px 24px 60px;
 grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{border:1px solid var(--line);border-radius:8px;background:var(--panel);
 padding:14px 16px}
.card h2{margin:0 0 2px;font-size:14px;font-weight:600}
.card .n{color:var(--muted);font-size:11.5px;margin-bottom:11px}
dl{display:grid;grid-template-columns:auto 1fr;gap:5px 12px;margin:0;font-size:12.5px}
dt{color:var(--muted);white-space:nowrap}
dd{margin:0;font-variant-numeric:tabular-nums}
.new{border-style:dashed;opacity:.72}
.new .why{color:var(--muted);font-size:12px;margin-top:8px}
.hint{margin-top:12px;padding-top:11px;border-top:1px dotted var(--line)}
.hint label{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}
.hint textarea{width:100%;font:inherit;font-size:12.5px;background:var(--panel2);
 color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:6px 8px;
 resize:vertical}
.hint .eg{color:var(--muted);font-size:11px;margin-top:5px;font-style:italic}
code{background:var(--panel2);padding:1px 5px;border-radius:3px;font-size:12px}
</style></head><body>
<header><h1>Vendor profiles — __CLIENT__</h1>
<div class="sub">Learned from every invoice imported so far. Nothing here was
configured by hand — it accumulates as invoices go through, and it is what the
checks compare each new invoice against.</div></header>
<div class="grid">__CARDS__</div>
<script>
// Hints are the only editable thing here. They are sentences rather than
// coordinates on purpose: a sentence survives a vendor redesigning their
// stationery and a scan arriving two degrees crooked.
document.addEventListener("change", e => {
  if(e.target.tagName!=="TEXTAREA") return;
  e.target.style.borderColor = "var(--accent)";
});
</script></body></html>"""


def render(profiles, out_path, client):
    def money(v):
        return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"

    cards = []
    for v in sorted(profiles):
        p = profiles[v]
        esc = html.escape
        if not p["established"]:
            cards.append(
                f'<div class="card new"><h2>{esc(v)}</h2>'
                f'<div class="n">{p["count"]} invoice'
                f'{"" if p["count"] == 1 else "s"} imported</div>'
                f'<div class="why">Too few invoices to expect anything yet. '
                f'Nothing is claimed about a vendor until there are '
                f'{MIN_PRIORS}, because one invoice is not a habit.</div></div>')
            continue
        rows = [
            ("Invoice numbers",
             f'<code>{esc(", ".join(p["number_shapes"]))}</code>'),
            ("Number range",
             f'{p["number_lo"]}–{p["number_hi"]}' if p["number_lo"] else "—"),
            ("Amounts", f'{money(p["total_lo"])} – {money(p["total_hi"])}'),
            ("Tax rate", (f'{p["tax_rate"]*100:.2f}%'
                          + ("" if p["tax_stable"] else " (varies)"))
             if p["tax_rate"] is not None else "—"),
            ("Freight", "usually charged" if p["charges_freight"] == "yes"
             else "not charged"),
            ("Currency", ", ".join(p["currencies"]) or "—"),
            ("Seen", f'{p["first_seen"]} to {p["last_seen"]}'),
            ("Corrections", str(p["corrections"])),
        ]
        dl = "".join(f"<dt>{k}</dt><dd>{val}</dd>" for k, val in rows)
        cards.append(
            f'<div class="card"><h2>{esc(v)}</h2>'
            f'<div class="n">{p["count"]} invoices imported across '
            f'{p["batches"]} batch{"" if p["batches"] == 1 else "es"}</div>'
            f'<dl>{dl}</dl>'
            f'<div class="hint"><label>Standing instruction for this vendor</label>'
            f'<textarea rows="2" data-vendor="{esc(v)}" '
            f'placeholder="none">{esc(p["hint"])}</textarea>'
            f'<div class="eg">e.g. "post Amount Due, not Total — they show '
            f'retainage"</div></div></div>')

    page = (PAGE.replace("__CARDS__", "".join(cards))
                .replace("__CLIENT__", html.escape(client)))
    from pathlib import Path
    Path(out_path).write_text(page, encoding="utf-8", newline="\n")
    return len(cards)
