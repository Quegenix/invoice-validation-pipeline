# Invoice Processing Pipeline

Ingest → extract → validate → route. PDFs in, posted records and a review
queue out.

> Commands below use `python3`. On Windows use `py` instead.

```
python3 pipeline/run.py                       # clean extraction
python3 pipeline/run.py --noise 0.30 --seed 11   # with realistic misreads
python3 pipeline/run.py --extractor claude    # real vision extraction
```

Open `out/review.html` afterward.

---

## The argument this pipeline makes

Extraction is probabilistic. A model reads a page and tells you what it thinks
it says, with no calibrated sense of whether it's right — it will report a
total with complete confidence whether it read it or invented it.

Validation is the opposite: cheap, deterministic, and answering a different
question. Not *did we read this correctly* but *is what we read internally
consistent and safe to post*.

That distinction is the whole product. Most tools in this space stop at
extraction and hand you a number. This one refuses to post anything that
doesn't reconcile.

**Run it with `--noise 0.30` and look at the result: 95.4% field accuracy,
and zero bad records posted.** Every misread got caught — not because the
extractor knew it had failed, but because the arithmetic didn't work. That is
the number worth putting in front of a client.

---

## The seven rules

Implemented in `validation.py`, which is readable as a spec.

| Rule | Catches |
|---|---|
| Line items sum to subtotal | Transposed digits, dropped lines, misread decimals — most of what extraction gets wrong |
| subtotal + tax + freight = total | Charges shown on the page but missing from what's being asked for |
| Vendor resolves to master list | Unknown senders, misread vendor names |
| Invoice number present | Documents that can't be duplicate-checked |
| Vendor + number not seen before | Double payment, the expensive AP failure |
| Date not more than a year old | Very late submissions, wrong-year keying errors |
| Date not in the future | Wrong year on the vendor's template |

Plus two advisory warnings that don't block: amount over threshold, and no
line items extracted.

Rules 1 and 2 are the ones to dwell on when explaining this to a client.
They're free, they need no model confidence score, and they catch the
majority of real extraction failures. **If the arithmetic doesn't reconcile,
the extraction is wrong** — you don't need to know why.

Rules 3 through 5 catch something different and arguably worse: documents that
are perfectly legible and still must not post. A duplicate invoice reads
flawlessly. No better extractor would ever help.

---

## Pieces

| File | Does |
|---|---|
| `extractors.py` | `MockExtractor` replays the manifest with optional injected misreads; `ClaudeExtractor` calls a vision model against the real PDF. One interface — the extractor is the part most likely to be swapped. |
| `validation.py` | The seven rules. Each finding carries a severity, a plain-language message, and the actual arithmetic. |
| `run.py` | Orchestration, routing, scoring, summary. |
| `review.py` | Builds `out/review.html` — page image beside extracted values, failed rule called out. |

### Why a mock extractor exists

It lets the entire pipeline be built, tested and demoed with no API key and no
per-page cost, and it makes the test suite deterministic. It also lets you
*dial in* extraction failure and prove the validation layer holds — which you
can't do reliably with a real model, because you can't make it fail on demand.

Injected misreads are not free passes. A misread amount fails the arithmetic
check and gets held, exactly as it should. From the pipeline's side a bad read
and a bad document look identical, and both need a human. That's correct.

### Real extraction

```
pip install anthropic
export ANTHROPIC_API_KEY=...
python3 pipeline/run.py --extractor claude
```

The prompt in `extractors.py` carries one instruction that matters more than
the rest:

> Transcribe, do not calculate. If the printed subtotal does not match what
> the line items add up to, return the printed subtotal anyway.

Without that, a capable model "helpfully" corrects the arithmetic and destroys
the signal the validation layer exists to detect. Worth knowing before you
point any model at financial documents.

---

## Two numbers, measuring different things

**Extraction accuracy** — did we read the page right? Scored against the
manifest, which records what is *printed*. An extractor that silently fixes a
bad subtotal scores worse here, and should.

**Routing** — did the right documents get held? Reported as defects missed
(bad records that posted — the failure that costs money) and holds without
cause (clean records held — the failure that costs patience). A pipeline that
posts everything is fast and wrong; one that holds everything is safe and
useless.

---

## Porting to n8n

The shape maps directly: a trigger watching a folder or inbox, an HTTP or
LLM node for extraction, a Function node carrying `validation.py`'s logic, an
IF node on `disposition`, then two branches. The review queue becomes a table
the team works from.

Build it here first. The rules are easier to get right in a place you can run
a test suite against.

---

## What's missing, deliberately

No accounting-system write yet — the destination is a thin adapter at the end,
and the hard part is everything before it. QuickBooks Desktop via qbXML, QBO
via REST, or a CSV import all attach at the same point.

No persistence. `seen_keys` is per-run, so duplicate detection only spans one
batch. Real deployment needs that ledger in a database.

No human-in-the-loop write-back. The review queue's buttons are inert — it
shows the decision a person would make, not a workflow that records it.
