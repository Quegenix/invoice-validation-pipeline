# Synthetic Invoice Test Set

24 fictitious vendor invoices as PDFs, in five visually distinct layouts, with
seven deliberately seeded data-quality defects and a ground-truth manifest.

Built as the input side of an invoice extraction + validation pipeline.

---

## Everything here is invented

No real vendor, customer, employer, person, address, phone number, tax ID or
bank detail appears anywhere in this set. Street names are fictional, phone
numbers use the 555 exchange reserved for fiction, and the buyer —
**Northgate Supply Co.** — does not exist.

That is deliberate. A portfolio piece built on real invoices exposes real
counterparties.

---

## What's in the box

```
invoices/        24 PDFs (three of them run to two pages)
manifest.json    ground truth for every document
vendors.csv      the known-good vendor master list, ready to import
README.md        this file
```

Alongside, in the parent directory:

```
dataset.py             companies, vendors, line-item catalogs
generate_invoices.py   the generator — re-run to get a fresh set
verify_testset.py      self-test of this set against the validation rules
```

`generate_invoices.py` is seeded (`SEED = 20260911`), so re-running reproduces
this exact set. Change the seed for a different one; change the `plan` list to
change the mix.

---

## The five layouts

| Layout | Count | What it exercises |
|---|---|---|
| `modern` | 8 | Clean digital PDF, colour header, zebra table. The easy case. |
| `classic` | 7 | Serif, ruled boxes, centred header, tax in a footer block. |
| `dense` | 4 | 22–41 line items, condensed type, spills to page 2 with a "continued" marker. Tests multi-page assembly. |
| `minimal` | 4 | No table rules at all. Quantity and unit price live on a second line under each description. Tests layouts with no grid to latch onto. |
| `scanned` | 3 | **No text layer.** Rendered to raster, then skewed, blurred, speckled, streaked and exposed unevenly. Tests the OCR/vision fallback path. |

Dates are printed in four different formats across the set — `08/17/2026`,
`2026-08-17`, `17-Aug-2026`, and `August 17, 2026` — because real AP inboxes
are not consistent. `date_format_on_document` in the manifest records which
one each invoice uses.

Tax and freight placement varies too: sometimes a footer block, sometimes an
ordinary line item, sometimes absent because the vendor doesn't charge it.

---

## The seven seeded defects

Each defect sits in exactly one document. Everything else about that document
is normal, so the defect is what a validator has to find.

| Document | Defect | Why it matters |
|---|---|---|
| INV-002 | `LINE_SUM_MISMATCH` | Printed subtotal is 9.00 higher than the line items sum to — a digit transposition. Tax and total are consistent *with the wrong subtotal*, so only the line-sum check catches it. |
| INV-006 | `STALE_DATE` | Dated over two years back. Should not post silently into the current period. |
| INV-009 | `UNKNOWN_VENDOR` | Vendor isn't on the master list. The document is internally perfect — only a lookup catches it. |
| INV-011 | `MISSING_INVOICE_NO` | No invoice number anywhere on the page. Nothing to duplicate-check against. |
| INV-014 | `DUPLICATE_INVOICE_NO` | Same vendor and invoice number as an earlier document, different date and amount. Posting both double-pays. |
| INV-017 | `TOTAL_MISMATCH` | Freight is charged but was never added into the total. |
| INV-020 | `FUTURE_DATE` | Dated months ahead — usually a wrong year on the vendor's template. |

Note what these have in common: **four of the seven are arithmetically or
referentially wrong while looking completely normal.** No amount of careful
reading catches `UNKNOWN_VENDOR` or `DUPLICATE_INVOICE_NO`. That's the case for
a validation layer rather than a better extractor.

17 of the 24 documents are clean. That ratio matters — a validator that flags
everything is as useless as one that flags nothing.

---

## How to read `manifest.json`

Fields ending in `_printed` record **what appears on the page**, not what would
have been arithmetically correct.

That distinction is the whole design. For INV-002, `subtotal_printed` is the
*wrong* subtotal, because that is what is physically printed on the invoice.

- A perfect extractor returns the wrong number, because that's what the
  document says.
- A correct validator then flags that it doesn't reconcile.

Measuring extraction against "what it should have been" would penalise an
extractor for reading the document correctly. Keep the two questions separate:
*did we read it right*, and *is what we read internally consistent*.

Each document also carries `expected_disposition` — `POST` or `REVIEW` — which
is the end-to-end answer the pipeline should arrive at.

---

## Verifying the set

> Commands below use `python3`. On Windows use `py` instead.

```
python3 verify_testset.py
```

Runs the reference rules against the manifest and asserts two things: every
seeded defect is caught, and no clean document is flagged. Current result is
7/7 and 17/17.

`pipeline/validation.py` is also readable as a spec. The rules it implements are the ones
the real pipeline should enforce:

1. Line items sum to the printed subtotal
2. `subtotal + tax + freight == total`
3. Vendor resolves against the master list
4. An invoice number is present
5. Vendor + invoice number hasn't been seen before
6. Invoice date is not more than a year old
7. Invoice date is not in the future

Rules 1 and 2 are worth dwelling on: they're free, deterministic, and they
catch most extraction errors without any confidence score from the model. If
the arithmetic doesn't reconcile, the extraction is wrong.

---

## Regenerating

```
python3 generate_invoices.py
```

Rewrites `out/` from scratch. To change the mix, edit the `plan` list — each
entry is `(layout, number_of_line_items)`. To move or change which defects are
seeded, edit the `targets` dict.
