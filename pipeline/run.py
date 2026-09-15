#!/usr/bin/env python3
"""
Invoice processing pipeline: ingest -> extract -> validate -> route.

    python3 pipeline/run.py                    # mock extraction, clean
    python3 pipeline/run.py --noise 0.25       # mock, with realistic misreads
    python3 pipeline/run.py --extractor claude # real vision extraction

Outputs:
    out/results.json     every document, what was read, what was found, where it went
    out/review.html      the review queue — flagged documents with their PDFs
    stdout               run summary

Two numbers matter and they measure different things:

  Extraction accuracy — did we read the page correctly? Measured against the
  manifest, which records what is PRINTED. An extractor that "helpfully"
  corrects a bad subtotal scores worse here, and should.

  Routing — did the right documents get held? A pipeline that posts everything
  is fast and wrong. One that holds everything is safe and useless.
"""

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from validation import validate, BLOCK, WARN          # noqa: E402
from extractors import MockExtractor, ClaudeExtractor  # noqa: E402

ROOT = Path(__file__).parent.parent
OUT = ROOT / "out"
PDF_DIR = OUT / "invoices"
MANIFEST = OUT / "manifest.json"


def norm_money(v):
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:
        return None


def score_extraction(extracted, truth):
    """Field-level comparison against what is printed on the page."""
    checks = []

    def cmp(field, got, want):
        checks.append((field, got == want, got, want))

    cmp("vendor_name", extracted.get("vendor_name"), truth["vendor_name"])
    cmp("invoice_number", extracted.get("invoice_number"), truth["invoice_number"])
    cmp("invoice_date", extracted.get("invoice_date"), truth["invoice_date"])
    cmp("subtotal", norm_money(extracted.get("subtotal")), norm_money(truth["subtotal_printed"]))
    cmp("tax", norm_money(extracted.get("tax")), norm_money(truth["tax_printed"]))
    cmp("freight", norm_money(extracted.get("freight")), norm_money(truth["freight_printed"]))
    cmp("total", norm_money(extracted.get("total")), norm_money(truth["total_printed"]))
    cmp("line_item_count", len(extracted.get("line_items") or []), truth["line_item_count"])

    got_lines = [norm_money(i.get("amount")) for i in (extracted.get("line_items") or [])]
    want_lines = [norm_money(i["amount"]) for i in truth["line_items"]]
    cmp("line_amounts", got_lines, want_lines)

    passed = sum(1 for _, ok, _, _ in checks if ok)
    return {
        "fields_checked": len(checks),
        "fields_correct": passed,
        "field_accuracy": round(passed / len(checks), 4),
        "mismatches": [
            {"field": f, "extracted": str(g), "printed": str(w)}
            for f, ok, g, w in checks if not ok
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=["mock", "claude"], default="mock")
    ap.add_argument("--noise", type=float, default=0.0,
                    help="mock only: probability of an injected misread")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text())
    truth = {d["file"]: d for d in manifest["documents"]}
    known_vendors = set(manifest["known_vendors"])
    today = date.fromisoformat(manifest["generated_on"])

    if args.extractor == "mock":
        extractor = MockExtractor(MANIFEST, noise=args.noise, seed=args.seed)
    else:
        extractor = ClaudeExtractor()

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {PDF_DIR}. Run generate_invoices.py first.")

    seen_keys = set()
    results = []

    for pdf in pdfs:
        record = extractor.extract(pdf)
        vr = validate(record, known_vendors=known_vendors,
                      seen_keys=seen_keys, today=today)
        t = truth[pdf.name]

        results.append({
            "file": pdf.name,
            "layout": t["layout"],
            "disposition": vr.disposition,
            "findings": [
                {"code": f.code, "severity": f.severity,
                 "message": f.message, "detail": f.detail}
                for f in vr.findings
            ],
            "extracted": record,
            "extraction_score": score_extraction(record, t),
            "injected_misread": getattr(extractor, "injected", {}).get(pdf.name),
            "seeded_defect": t["seeded_error"],
            "expected_disposition": t["expected_disposition"],
        })

    # ---- routing correctness ------------------------------------------------
    posted = [r for r in results if r["disposition"] == "POST"]
    held = [r for r in results if r["disposition"] == "REVIEW"]

    # A hold is correct if the document had a seeded defect OR extraction
    # genuinely went wrong on it. Both are real reasons to want a human.
    correct_holds = [r for r in held
                     if r["seeded_defect"] or r["injected_misread"]]
    false_holds = [r for r in held if r not in correct_holds]
    missed = [r for r in posted if r["seeded_defect"]]

    total_fields = sum(r["extraction_score"]["fields_checked"] for r in results)
    ok_fields = sum(r["extraction_score"]["fields_correct"] for r in results)

    summary = {
        "extractor": extractor.name,
        "noise": args.noise if args.extractor == "mock" else None,
        "documents": len(results),
        "posted": len(posted),
        "held_for_review": len(held),
        "holds_with_a_real_cause": len(correct_holds),
        "holds_without_cause": len(false_holds),
        "defects_missed": len(missed),
        "extraction_field_accuracy": round(ok_fields / total_fields, 4),
        "fields_checked": total_fields,
    }

    (OUT / "results.json").write_text(
        json.dumps({"summary": summary, "documents": results}, indent=2),
        newline="\n", encoding="utf-8")

    # ---- report -------------------------------------------------------------
    print()
    print(f"  extractor            {extractor.name}"
          + (f"   noise={args.noise}" if args.extractor == "mock" else ""))
    print(f"  documents            {len(results)}")
    print()
    print(f"  posted               {len(posted)}")
    print(f"  held for review      {len(held)}")
    print()
    print(f"  extraction accuracy  {summary['extraction_field_accuracy'] * 100:.1f}%"
          f"  ({ok_fields}/{total_fields} fields)")
    print(f"  defects missed       {len(missed)}")
    print(f"  holds without cause  {len(false_holds)}")
    print()

    if held:
        print("  HELD")
        for r in held:
            why = r["findings"][0]["code"] if r["findings"] else "?"
            src = ("seeded: " + r["seeded_defect"]) if r["seeded_defect"] else \
                  ("misread: " + r["injected_misread"][:44]) if r["injected_misread"] else \
                  "no known cause"
            print(f"    {r['file']:<12} {r['layout']:<9} {why:<22} {src}")
        print()

    if missed:
        print("  MISSED — defect present but posted anyway")
        for r in missed:
            print(f"    {r['file']}  {r['seeded_defect']}")
        print()

    import review
    review.build(OUT / "results.json", OUT / "review.html", manifest)
    print(f"  review queue         {OUT / 'review.html'}")
    print(f"  full results         {OUT / 'results.json'}")
    print()

    return 1 if (missed or false_holds) else 0


if __name__ == "__main__":
    sys.exit(main())
