"""
Extraction layer.

Two implementations behind one interface:

  MockExtractor   replays the manifest, optionally injecting realistic
                  misreads. Lets the entire pipeline be built, tested and
                  demoed with no API key and no per-page cost.

  ClaudeExtractor calls a vision model against the actual PDF.

The interface is deliberately thin — `extract(pdf_path) -> dict` — because
the extractor is the part most likely to be swapped. Today a general vision
model; tomorrow a purpose-built invoice service that returns per-field
confidence scores. Everything downstream should not care.

Note what the extractor does NOT do: it does not decide whether the numbers
are right. It reports what it believes the page says. Judgement lives in
validation.py.
"""

import json
import os
import random
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# The contract. Both extractors return this shape.
# ---------------------------------------------------------------------------

SCHEMA = {
    "vendor_name": "str — exactly as printed, no normalisation",
    "invoice_number": "str | null",
    "invoice_date": "str — ISO 8601 (YYYY-MM-DD), converted from whatever "
                    "format the document used",
    "due_date": "str | null — ISO 8601",
    "terms": "str | null",
    "po_number": "str | null",
    "currency": "str — ISO code, default USD",
    "bill_to_name": "str | null — the company this invoice is addressed to, "
                    "exactly as printed in the bill-to block. Null if the "
                    "document does not show one.",
    "tax_rate_printed": "string decimal | null — the tax RATE if the invoice "
                        "prints one (a line like 'Tax 6.00%' or 'GST @ 5%'). "
                        "Return it as printed, e.g. '6.00' or '0.06'. Null if "
                        "only a tax amount is shown and no rate.",
    "line_items": [
        {
            "description": "str",
            "quantity": "number",
            "unit": "str | null",
            "unit_price": "string decimal",
            "amount": "string decimal — the extended amount for the line",
        }
    ],
    "subtotal": "string decimal — as printed, do not recompute",
    "discount": "string decimal — any discount or credit deducted before tax, "
                "as a POSITIVE number. 0 if absent.",
    "fees": "string decimal — surcharges, trip fees, environmental or card "
            "fees added to the bill. 0 if absent.",
    "amount_paid": "string decimal — deposits or payments already applied to "
                   "this invoice. 0 if absent.",
    "balance_due": "string decimal | null — the amount actually owed now, if "
                   "the invoice states one separately from the total.",
    "tax": "string decimal — as printed, 0 if absent",
    "freight": "string decimal — as printed, 0 if absent",
    "total": "string decimal — as printed, do not recompute",
}


EXTRACTION_PROMPT = """\
You are reading a vendor invoice. Return the values that are PRINTED on the \
document.

Critical rule: transcribe, do not calculate. If the printed subtotal does not \
match what the line items add up to, return the printed subtotal anyway. \
Detecting that disagreement is a downstream job, and silently "fixing" it \
here destroys the signal.

Other rules:
- Convert all dates to ISO 8601 (YYYY-MM-DD). The document may use any format.
- Return monetary values as plain decimal strings: "1234.56", no currency \
symbols, no thousands separators.
- If a field is genuinely absent from the document, return null. Do not guess \
and do not substitute a default.
- Capture every line item, including any that continue onto later pages.
- Return the vendor name exactly as printed.

Return only JSON matching this schema:
%s
""" % json.dumps(SCHEMA, indent=2)


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

# Character confusions a real OCR pass makes on a degraded scan.
CONFUSIONS = [
    ("0", "O"), ("O", "0"), ("1", "l"), ("l", "1"), ("5", "S"), ("S", "5"),
    ("8", "B"), ("B", "8"), ("rn", "m"), ("m", "rn"), ("6", "b"),
]


class MockExtractor:
    """
    Replays manifest ground truth, so downstream work is testable without an
    API key.

    `noise` is the probability that a given document picks up a misread.
    Errors are weighted toward the `scanned` layout, which is where real
    extraction actually degrades — a clean digital PDF very rarely produces a
    wrong number, a faxed one regularly does.

    Injected errors are NOT free passes. A misread amount will fail the
    arithmetic check, and it should — that is the validation layer doing its
    job against a bad read rather than a bad document. Those two failure
    modes look identical from the pipeline's side, and both need a human.
    """

    name = "mock"

    def __init__(self, manifest_path, noise=0.0, seed=7):
        m = json.loads(Path(manifest_path).read_text())
        self.docs = {d["file"]: d for d in m["documents"]}
        self.noise = noise
        self.rng = random.Random(seed)
        self.injected = {}

    def _fuzz_text(self, s):
        if not s:
            return s
        for a, b in self.rng.sample(CONFUSIONS, len(CONFUSIONS)):
            if a in s:
                return s.replace(a, b, 1)
        return s

    def extract(self, pdf_path):
        key = Path(pdf_path).name
        doc = self.docs[key]

        rec = {
            "vendor_name": doc["vendor_name"],
            "invoice_number": doc["invoice_number"],
            "invoice_date": doc["invoice_date"],
            "due_date": doc["due_date"],
            "terms": doc["terms"],
            "po_number": doc["po_number"],
            "currency": doc["currency"],
            "line_items": [dict(i) for i in doc["line_items"]],
            "subtotal": doc["subtotal_printed"],
            "tax": doc["tax_printed"],
            "freight": doc["freight_printed"],
            "total": doc["total_printed"],
        }

        # Scans misread far more often than clean PDFs do.
        weight = 3.5 if doc["layout"] == "scanned" else 1.0
        if self.rng.random() < self.noise * weight:
            mode = self.rng.choice(["amount", "vendor", "drop_line"])

            if mode == "amount" and rec["line_items"]:
                i = self.rng.randrange(len(rec["line_items"]))
                orig = rec["line_items"][i]["amount"]
                # Transpose two digits — the classic misread
                digits = re.sub(r"[^\d]", "", orig)
                if len(digits) >= 3:
                    lst = list(orig)
                    pos = [k for k, ch in enumerate(lst) if ch.isdigit()]
                    a, b = self.rng.sample(pos, 2)
                    lst[a], lst[b] = lst[b], lst[a]
                    rec["line_items"][i]["amount"] = "".join(lst)
                    self.injected[key] = (
                        f"line {i + 1} amount misread as "
                        f"{rec['line_items'][i]['amount']} (printed: {orig})"
                    )

            elif mode == "vendor":
                orig = rec["vendor_name"]
                rec["vendor_name"] = self._fuzz_text(orig)
                if rec["vendor_name"] != orig:
                    self.injected[key] = (
                        f"vendor misread as '{rec['vendor_name']}' "
                        f"(printed: '{orig}')"
                    )

            elif mode == "drop_line" and len(rec["line_items"]) > 3:
                i = self.rng.randrange(len(rec["line_items"]))
                dropped = rec["line_items"].pop(i)
                self.injected[key] = (
                    f"line {i + 1} dropped entirely "
                    f"({dropped['description'][:40]}, {dropped['amount']})"
                )

        return rec


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------

class ClaudeExtractor:
    """
    Sends the PDF to a vision model and parses the JSON back.

    Requires:  pip install anthropic
               export ANTHROPIC_API_KEY=...

    Deliberately has no retry-on-garbage or self-correction logic. If the
    model returns something unusable, that should surface as a failure the
    validation layer catches, not get quietly patched over here.
    """

    name = "claude"

    def __init__(self, model="claude-sonnet-5", max_tokens=8000):
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "pip install anthropic, then set ANTHROPIC_API_KEY"
            )
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.injected = {}

    def extract(self, pdf_path):
        import base64

        data = base64.standard_b64encode(Path(pdf_path).read_bytes()).decode()

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": data,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }],
        )

        # The response may lead with thinking blocks, so take the first
        # actual text block rather than assuming index 0.
        blocks = [b for b in msg.content if getattr(b, "type", None) == "text"]
        if not blocks:
            raise RuntimeError(
                f"No text block in the response for {Path(pdf_path).name}; "
                f"got {[getattr(b, 'type', '?') for b in msg.content]}")
        text = blocks[0].text.strip()
        # Models like to wrap JSON in fences regardless of instruction.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return json.loads(text)
