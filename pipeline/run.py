#!/usr/bin/env python3
"""
Invoice processing pipeline: ingest -> extract -> validate -> route.

    python3 pipeline/run.py                    # mock extraction, clean
    python3 pipeline/run.py --noise 0.25       # mock, with realistic misreads
    python3 pipeline/run.py --extractor claude # real vision extraction

Against a real client folder — no manifest, no ground truth:

    python3 pipeline/run.py --extractor claude
        --input "C:/Invoice Cleanup/Acme/invoices"
        --vendors approved_vendors.csv
        --client-name "Acme Mechanical"

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

Both of those are scored against the manifest, which only describes the
generated test set. Point --input at anything else and the manifest does not
apply: the pipeline runs in client mode, reports routing only, and omits every
number it cannot honestly compute. Reporting demo ground truth against client
documents would be worse than crashing.
"""

import argparse
import csv
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
DEMO_PDF_DIR = OUT / "invoices"
MANIFEST = OUT / "manifest.json"


def load_vendor_csv(path):
    """
    Approved-vendor list. First column is the vendor name; everything else is
    ignored, so a full vendor export works as-is. A header row naming the first
    column something vendor-ish is skipped.
    """
    names = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if row and row[0].strip():
                names.append(row[0].strip())
    if names and names[0].lower().replace(" ", "").replace("_", "") in (
            "vendorname", "vendor", "name", "payee", "supplier", "suppliername"):
        names.pop(0)
    if not names:
        sys.exit(f"No vendor names in first column of {path}.")
    return set(names)


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
    ap.add_argument("--input", metavar="DIR",
                    help="folder of PDFs to process (default: the generated "
                         "test set in out/invoices)")
    ap.add_argument("--vendors", metavar="CSV",
                    help="approved vendor list; first column is the vendor "
                         "name. Replaces the manifest's known_vendors.")
    ap.add_argument("--client-name", metavar="NAME",
                    help="name shown on the review page header")
    args = ap.parse_args()

    pdf_dir = Path(args.input).expanduser() if args.input else DEMO_PDF_DIR
    # The manifest describes exactly one folder. Anywhere else, it does not apply.
    demo = pdf_dir.resolve() == DEMO_PDF_DIR.resolve()

    manifest = None
    truth = {}

    if demo:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        truth = {d["file"]: d for d in manifest["documents"]}
        known_vendors = set(manifest["known_vendors"])
        today = date.fromisoformat(manifest["generated_on"])
        buyer = args.client_name or manifest["buyer"]
    else:
        # No manifest: no ground truth, and no pretending otherwise.
        known_vendors = set()
        today = date.today()
        buyer = args.client_name or pdf_dir.name

        if args.extractor == "mock":
            sys.exit(
                f"--extractor mock cannot run against {pdf_dir}.\n"
                "MockExtractor does not read PDFs - it replays "
                "out/manifest.json, which only\n"
                "describes the generated test set. There is nothing for it "
                "to replay here.\n"
                "Use --extractor claude.")

    if args.vendors:
        known_vendors = load_vendor_csv(Path(args.vendors).expanduser())
    elif not demo:
        print(f"\n  WARNING: no --vendors given, so no vendor is approved and "
              f"every document\n           will be held as UNKNOWN_VENDOR. "
              f"Pass --vendors to get real routing.")

    if args.extractor == "mock":
        extractor = MockExtractor(MANIFEST, noise=args.noise, seed=args.seed)
    else:
        extractor = ClaudeExtractor()

    if not pdf_dir.is_dir():
        sys.exit(f"Not a directory: {pdf_dir}")

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {pdf_dir}."
                 + (" Run generate_invoices.py first." if demo else ""))

    seen_keys = set()
    results = []

    for pdf in pdfs:
        record = extractor.extract(pdf)
        vr = validate(record, known_vendors=known_vendors,
                      seen_keys=seen_keys, today=today)

        # Manifest-derived fields are omitted in client mode rather than
        # defaulted — a 0 that looks like a measurement is the failure mode
        # being avoided. Key order matches the demo output it replaces.
        t = truth.get(pdf.name)

        row = {"file": pdf.name}
        if demo:
            row["layout"] = t["layout"]
        row["disposition"] = vr.disposition
        row["findings"] = [
            {"code": f.code, "severity": f.severity,
             "message": f.message, "detail": f.detail}
            for f in vr.findings
        ]
        row["extracted"] = record
        if demo:
            row["extraction_score"] = score_extraction(record, t)
            row["injected_misread"] = getattr(extractor, "injected", {}).get(pdf.name)
            row["seeded_defect"] = t["seeded_error"]
            row["expected_disposition"] = t["expected_disposition"]

        results.append(row)

    # ---- routing ------------------------------------------------------------
    posted = [r for r in results if r["disposition"] == "POST"]
    held = [r for r in results if r["disposition"] == "REVIEW"]

    summary = {
        "extractor": extractor.name,
        "noise": args.noise if args.extractor == "mock" else None,
        "scored_against_manifest": demo,
        "documents": len(results),
        "posted": len(posted),
        "held_for_review": len(held),
    }

    if not demo:
        # Recorded off the demo path only: an absolute local path in the
        # committed out/results.json would differ on every machine.
        summary["input"] = str(pdf_dir.resolve())

    correct_holds = false_holds = missed = []
    ok_fields = total_fields = 0

    if demo:
        # A hold is correct if the document had a seeded defect OR extraction
        # genuinely went wrong on it. Both are real reasons to want a human.
        correct_holds = [r for r in held
                         if r["seeded_defect"] or r["injected_misread"]]
        false_holds = [r for r in held if r not in correct_holds]
        missed = [r for r in posted if r["seeded_defect"]]

        total_fields = sum(r["extraction_score"]["fields_checked"] for r in results)
        ok_fields = sum(r["extraction_score"]["fields_correct"] for r in results)

        summary.update({
            "holds_with_a_real_cause": len(correct_holds),
            "holds_without_cause": len(false_holds),
            "defects_missed": len(missed),
            "extraction_field_accuracy": round(ok_fields / total_fields, 4),
            "fields_checked": total_fields,
        })

    (OUT / "results.json").write_text(
        json.dumps({"summary": summary, "documents": results}, indent=2),
        newline="\n", encoding="utf-8")

    # ---- report -------------------------------------------------------------
    print()
    print(f"  extractor            {extractor.name}"
          + (f"   noise={args.noise}" if args.extractor == "mock" else ""))
    if not demo:
        print(f"  input                {pdf_dir}")
        print(f"  approved vendors     {len(known_vendors)}")
    print(f"  documents            {len(results)}")
    print()
    print(f"  posted               {len(posted)}")
    print(f"  held for review      {len(held)}")
    print()

    if demo:
        print(f"  extraction accuracy  {summary['extraction_field_accuracy'] * 100:.1f}%"
              f"  ({ok_fields}/{total_fields} fields)")
        print(f"  defects missed       {len(missed)}")
        print(f"  holds without cause  {len(false_holds)}")
    else:
        print("  no manifest for this folder: extraction accuracy and defect")
        print("  counts are not reported, because nothing here can measure them.")
    print()

    if held:
        print("  HELD")
        for r in held:
            why = r["findings"][0]["code"] if r["findings"] else "?"
            if demo:
                src = ("seeded: " + r["seeded_defect"]) if r["seeded_defect"] else \
                      ("misread: " + r["injected_misread"][:44]) if r["injected_misread"] else \
                      "no known cause"
                print(f"    {r['file']:<12} {r['layout']:<9} {why:<22} {src}")
            else:
                vendor = str(r["extracted"].get("vendor_name") or "?")[:28]
                print(f"    {r['file']:<28} {why:<22} {vendor}")
        print()

    if missed:
        print("  MISSED — defect present but posted anyway")
        for r in missed:
            print(f"    {r['file']}  {r['seeded_defect']}")
        print()

    import review
    review.build(OUT / "results.json", OUT / "review.html",
                 buyer=buyer, pdf_dir=pdf_dir, demo=demo)
    print(f"  review queue         {OUT / 'review.html'}")
    print(f"  full results         {OUT / 'results.json'}")
    print()

    return 1 if (missed or false_holds) else 0


if __name__ == "__main__":
    sys.exit(main())
