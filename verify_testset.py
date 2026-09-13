#!/usr/bin/env python3
"""
Reference validation layer, run against the manifest's ground truth.

This is not the pipeline. It exists to prove two things about the generated
test set:

  1. Every seeded defect is actually detectable by a deterministic rule.
     If a rule can't catch it, the test case is decoration.
  2. No clean invoice trips a rule. A validator that flags everything is
     as useless as one that flags nothing.

The rules here are the same ones the real pipeline should implement, which
makes this file a spec as much as a test.
"""

import json
import sys
from decimal import Decimal
from datetime import date
from pathlib import Path

MANIFEST = Path(__file__).parent / "out" / "manifest.json"

STALE_DAYS = 365
FUTURE_DAYS = 1


def D(s):
    return Decimal(s)


def validate(doc, known_vendors, seen, today):
    """Return a list of failure codes for one extracted document."""
    fails = []

    # 1. Line items must sum to the printed subtotal.
    if D(doc["sum_of_line_amounts"]) != D(doc["subtotal_printed"]):
        fails.append("LINE_SUM_MISMATCH")

    # 2. subtotal + tax + freight must equal the printed total.
    expected = (D(doc["subtotal_printed"])
                + D(doc["tax_printed"])
                + D(doc["freight_printed"]))
    if expected != D(doc["total_printed"]):
        fails.append("TOTAL_MISMATCH")

    # 3. Vendor must resolve against the master list.
    if doc["vendor_name"] not in known_vendors:
        fails.append("UNKNOWN_VENDOR")

    # 4. An invoice number must be present.
    if not doc["invoice_number"]:
        fails.append("MISSING_INVOICE_NO")
    else:
        # 5. ...and must not already exist for this vendor.
        key = (doc["vendor_name"], doc["invoice_number"])
        if key in seen:
            fails.append("DUPLICATE_INVOICE_NO")
        seen.add(key)

    # 6. Date sanity.
    d = date.fromisoformat(doc["invoice_date"])
    age = (today - d).days
    if age > STALE_DAYS:
        fails.append("STALE_DATE")
    if age < -FUTURE_DAYS:
        fails.append("FUTURE_DATE")

    return fails


def main():
    m = json.loads(MANIFEST.read_text())
    known = set(m["known_vendors"])
    today = date.fromisoformat(m["generated_on"])

    seen = set()
    problems = []
    caught = 0
    clean_ok = 0

    print(f"{'DOC':<9} {'LAYOUT':<9} {'SEEDED':<22} {'DETECTED':<40} RESULT")
    print("-" * 100)

    for doc in m["documents"]:
        fails = validate(doc, known, seen, today)
        seeded = doc["seeded_error"]
        detected = ",".join(fails) if fails else "-"

        if seeded:
            if seeded in fails:
                result = "OK  caught"
                caught += 1
                extra = [f for f in fails if f != seeded]
                if extra:
                    result += f" (+{len(extra)} incidental)"
            else:
                result = "FAIL  missed"
                problems.append((doc["file"], f"seeded {seeded} not detected"))
        else:
            if fails:
                result = "FAIL  false positive"
                problems.append((doc["file"], f"clean doc flagged {fails}"))
            else:
                result = "OK  clean"
                clean_ok += 1

        print(f"{doc['file']:<9} {doc['layout']:<9} {str(seeded or '-'):<22} "
              f"{detected:<40} {result}")

    print("-" * 100)
    print(f"seeded defects caught : {caught}/{m['seeded_count']}")
    print(f"clean docs passed     : {clean_ok}/{m['clean_count']}")

    if problems:
        print("\nPROBLEMS")
        for f, why in problems:
            print(f"  {f}: {why}")
        return 1

    print("\nTest set is sound: every seeded defect is detectable, "
          "no clean document is flagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
