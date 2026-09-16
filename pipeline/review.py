"""
Builds the review queue as a single self-contained HTML file.

This is the part of the pipeline a client actually looks at, and it is the
reason the demo lands. Everyone can show "AI read my invoice." Very few can
show what happens when the AI reads it wrong — and that is the only question
a bookkeeper genuinely cares about.

Design decisions worth keeping if this gets rewritten:

  * Page image beside the extracted values. The whole job of this screen is
    letting a human answer "is that what it says?" in about two seconds. Any
    layout that makes them click to see the source has failed.

  * The failed rule is stated in plain language with the actual arithmetic
    shown. "Line items total 6,189.59, subtotal says 6,198.59" needs no
    explanation. "VALIDATION_ERROR_2" needs a support call.

  * Page images are embedded as data URIs so the file works when emailed,
    dropped in Slack, or opened from a thumb drive with no server running.
"""

import base64
import html
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

SEVERITY_ORDER = {"BLOCK": 0, "WARN": 1}


def page_png(pdf_path: Path, dpi=105) -> str:
    """
    First page of a PDF as a base64 PNG data URI, or "" if it cannot be
    rendered for any reason.

    This never raises. It is called at the very end of a run, after the
    extraction has already been paid for, and a report with missing page
    images is worth far more than a traceback that loses the whole run.
    Poppler absent, a file that moved, a PDF the renderer chokes on, a
    pathological page that would hang — all degrade to no image.
    """
    if not shutil.which("pdftoppm") or not Path(pdf_path).is_file():
        return ""
    try:
        with tempfile.TemporaryDirectory() as td:
            stem = Path(td) / "pg"
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                 str(pdf_path), str(stem)],
                check=True, capture_output=True, timeout=30,
            )
            pngs = sorted(Path(td).glob("pg*.png"))
            if not pngs:
                return ""
            return "data:image/png;base64," + base64.b64encode(
                pngs[0].read_bytes()).decode()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError):
        return ""


def money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return html.escape(str(v)) if v is not None else "—"


def build(results_path, out_path, buyer, pdf_dir=None, demo=True):
    """
    `demo` says whether the run was scored against the manifest. When it is
    False there is no ground truth, so the accuracy and defect columns are
    left out entirely rather than rendered as zeroes.
    """
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    summary = data["summary"]
    docs = data["documents"]
    # None means no images. It used to fall back to a guessed directory,
    # which in client mode quietly rendered the demo invoices next to the
    # client's numbers.
    pdf_dir = Path(pdf_dir) if pdf_dir else None

    held = [d for d in docs if d["disposition"] == "REVIEW"]
    posted = [d for d in docs if d["disposition"] == "POST"]

    cards = []
    no_image = 0
    for d in held:
        img = page_png(pdf_dir / d["file"]) if pdf_dir else ""
        if not img:
            no_image += 1
        ex = d["extracted"]
        findings = sorted(d["findings"],
                          key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))

        finding_html = "".join(
            f'''<div class="finding {f['severity'].lower()}">
                  <div class="fhead"><span class="sev">{f['severity']}</span>
                    <span class="code">{html.escape(f['code'])}</span></div>
                  <div class="fmsg">{html.escape(f['message'])}</div>
                  {f'<div class="fdetail">{html.escape(f["detail"])}</div>' if f['detail'] else ''}
                </div>'''
            for f in findings
        )

        items = ex.get("line_items") or []
        rows = "".join(
            f'''<tr><td class="desc">{html.escape(str(i.get('description', ''))[:56])}</td>
                    <td class="num">{html.escape(str(i.get('quantity', '')))}</td>
                    <td class="num">{money(i.get('unit_price'))}</td>
                    <td class="num">{money(i.get('amount'))}</td></tr>'''
            for i in items[:14]
        )
        if len(items) > 14:
            rows += (f'<tr><td colspan="4" class="more">'
                     f'+ {len(items) - 14} more lines</td></tr>')

        line_total = sum(
            float(i.get("amount") or 0) for i in items
        )

        cause = ""
        if d.get("injected_misread"):
            cause = (f'<div class="cause misread"><b>Simulated misread:</b> '
                     f'{html.escape(d["injected_misread"])}</div>')
        elif d.get("seeded_defect"):
            cause = (f'<div class="cause seeded"><b>Seeded into the test set:</b> '
                     f'{html.escape(d["seeded_defect"])}</div>')

        cards.append(f'''
<section class="card">
  <div class="card-head">
    <h2>{html.escape(d["file"])}</h2>
    {f'<span class="layout">{html.escape(d["layout"])} layout</span>' if d.get("layout") else ''}
    <span class="badge hold">HELD</span>
  </div>
  <div class="split">
    <div class="page">
      {'<img src="' + img + '" alt="invoice page">' if img else '<div class="noimg">page image unavailable</div>'}
    </div>
    <div class="detail">
      {finding_html}
      {cause}
      <div class="fields">
        <div class="f"><label>Vendor</label><span>{html.escape(str(ex.get('vendor_name') or '—'))}</span></div>
        <div class="f"><label>Invoice no.</label><span>{html.escape(str(ex.get('invoice_number') or '—'))}</span></div>
        <div class="f"><label>Date</label><span>{html.escape(str(ex.get('invoice_date') or '—'))}</span></div>
        <div class="f"><label>Terms</label><span>{html.escape(str(ex.get('terms') or '—'))}</span></div>
      </div>
      <table class="lines">
        <thead><tr><th>Description</th><th class="num">Qty</th>
                   <th class="num">Unit</th><th class="num">Amount</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <table class="totals">
        <tr><td>Sum of lines read</td><td class="num calc">{line_total:,.2f}</td></tr>
        <tr><td>Subtotal on document</td><td class="num">{money(ex.get('subtotal'))}</td></tr>
        <tr><td>Tax</td><td class="num">{money(ex.get('tax'))}</td></tr>
        <tr><td>Freight</td><td class="num">{money(ex.get('freight'))}</td></tr>
        <tr class="grand"><td>Total on document</td><td class="num">{money(ex.get('total'))}</td></tr>
      </table>
      <div class="actions">
        <button class="approve">Approve &amp; post</button>
        <button class="fix">Correct values</button>
        <button class="reject">Reject</button>
      </div>
    </div>
  </div>
</section>''')

    # A document can post and still carry a warning — most often a vendor
    # name that resolved to the master list from a different printing. That
    # resolution has to be visible somewhere, and the posted table is the
    # only place anyone will see it, because by definition nobody is going
    # to open these one at a time.
    def posted_note(p):
        notes = []
        for f in p.get("findings", []):
            if f.get("severity") != "WARN":
                continue
            if f["code"] == "VENDOR_NAME_VARIANT":
                printed = p["extracted"].get("vendor_name") or ""
                notes.append(f'printed as "{printed}"')
            else:
                notes.append(f["message"])
        return "; ".join(notes)

    notes = {p["file"]: posted_note(p) for p in posted}
    any_notes = any(notes.values())

    posted_rows = "".join(
        f'''<tr><td>{html.escape(p["file"])}</td>
                {f'<td>{html.escape(p["layout"])}</td>' if demo else ''}
                <td>{html.escape(str(p["extracted"].get("vendor_name_resolved")
                                     or p["extracted"].get("vendor_name") or "—"))}</td>
                <td class="num">{money(p["extracted"].get("total"))}</td>
                {f'<td class="note">{html.escape(notes[p["file"]])}</td>' if any_notes else ''}
                {f'<td class="num acc">{p["extraction_score"]["field_accuracy"] * 100:.0f}%</td>' if demo else ''}</tr>'''
        for p in posted
    )

    # If page images are missing, say so. A reviewer looking at a card with
    # an empty pane has no way to tell whether the document is blank or the
    # renderer is. Silent degradation is the one failure this report cannot
    # afford, since its entire job is making a machine's reading checkable
    # against the page.
    degraded = ""
    if held and no_image:
        if not shutil.which("pdftoppm"):
            why = ("poppler is not installed, so no page images could be "
                   "rendered — install it to compare each reading against "
                   "the document it came from")
        elif not pdf_dir:
            why = "no source directory was given, so page images were skipped"
        else:
            why = (f"{no_image} of {len(held)} pages could not be rendered "
                   f"from {pdf_dir}")
        degraded = (f'<div class="degraded"><strong>Page images '
                    f'unavailable</strong> — {html.escape(why)}.</div>')

    posted_head = ("<th>File</th>"
                   + ("<th>Layout</th>" if demo else "")
                   + "<th>Vendor</th><th class=\"num\">Total</th>"
                   + ("<th>Note</th>" if any_notes else "")
                   + ("<th class=\"num\">Field acc.</th>" if demo else ""))

    # Ground-truth tiles only exist when there is ground truth.
    if demo:
        acc = summary["extraction_field_accuracy"] * 100
        truth_stats = (
            f'<div class="stat"><div class="n">{acc:.1f}%</div>'
            f'<div class="l">Field accuracy</div></div>'
            f'<div class="stat"><div class="n">{summary["defects_missed"]}</div>'
            f'<div class="l">Defects missed</div></div>'
            f'<div class="stat"><div class="n">{summary["holds_without_cause"]}</div>'
            f'<div class="l">Holds w/o cause</div></div>')
        footer_note = (
            "Every vendor, amount and address in this queue is fictitious test data.")
    else:
        truth_stats = ""
        footer_note = (
            "This run had no manifest, so extraction accuracy and defect counts "
            "are not shown — there is nothing to measure them against. Routing "
            "below is the pipeline's own judgement, not a scored result.")

    doc = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AP Review Queue — {html.escape(str(buyer))}</title>
<style>
:root {{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#16191d; --muted:#6b7280;
  --line:#e3e6ea; --block:#b42318; --block-bg:#fef3f2; --warn:#b54708;
  --warn-bg:#fffaeb; --ok:#067647; --ok-bg:#ecfdf3; --accent:#21506f;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#0f1216; --panel:#171b21; --ink:#e6e8eb; --muted:#9aa3ad;
    --line:#252b33; --block:#f97066; --block-bg:#2a1614; --warn:#fdb022;
    --warn-bg:#2a2012; --ok:#47cd89; --ok-bg:#0f2419; --accent:#7fb3d5;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; padding-block:28px; padding-left:20px; padding-right:20px; }}
header h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-.01em; }}
header .sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:26px; }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:9px;
  padding:11px 15px; min-width:120px; flex:1 1 120px; }}
.stat .n {{ font-size:22px; font-weight:650; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; }}
.stat .l {{ font-size:11px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.05em; margin-top:2px; }}
.stat.hold .n {{ color:var(--block); }}
.stat.ok .n {{ color:var(--ok); }}
h3.sec {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:30px 0 12px; font-weight:600; }}
.card {{ background:var(--panel); border:1px solid var(--line);
  border-radius:11px; margin-bottom:18px; overflow:hidden; }}
.card-head {{ display:flex; align-items:center; gap:11px; padding:13px 17px;
  border-bottom:1px solid var(--line); flex-wrap:wrap; }}
.card-head h2 {{ font-size:15px; margin:0; font-weight:620; }}
.layout {{ font-size:11px; color:var(--muted); }}
.badge {{ margin-left:auto; font-size:10.5px; font-weight:700; letter-spacing:.07em;
  padding:3px 9px; border-radius:5px; }}
.badge.hold {{ background:var(--block-bg); color:var(--block); }}
.split {{ display:grid; grid-template-columns:minmax(0,0.92fr) minmax(0,1.08fr); }}
.page {{ border-right:1px solid var(--line); padding:15px; background:var(--bg); }}
.page img {{ width:100%; max-width:100%; display:block; border:1px solid var(--line);
  border-radius:5px; background:#fff; }}
.noimg {{ color:var(--muted); font-size:12px; padding:40px; text-align:center; }}
.detail {{ padding:15px 17px; min-width:0; }}
.finding {{ border-radius:8px; padding:10px 12px; margin-bottom:9px;
  border:1px solid transparent; }}
.finding.block {{ background:var(--block-bg); border-color:var(--block); }}
.finding.warn {{ background:var(--warn-bg); border-color:var(--warn); }}
.fhead {{ display:flex; gap:8px; align-items:baseline; margin-bottom:3px; }}
.sev {{ font-size:10px; font-weight:800; letter-spacing:.07em; }}
.finding.block .sev {{ color:var(--block); }}
.finding.warn .sev {{ color:var(--warn); }}
.code {{ font:11px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); }}
.fmsg {{ font-weight:560; font-size:13px; }}
.fdetail {{ font:12px ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--muted); margin-top:5px; line-height:1.45; }}
.cause {{ font-size:11.5px; padding:7px 10px; border-radius:6px;
  margin-bottom:11px; color:var(--muted); border:1px dashed var(--line); }}
.fields {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr));
  gap:7px 14px; margin:13px 0; }}
.f label {{ display:block; font-size:10px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); }}
.f span {{ font-size:13px; }}
table {{ width:100%; border-collapse:collapse; margin:11px 0; }}
th {{ text-align:left; font-size:10px; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); font-weight:600;
  padding:5px 6px; border-bottom:1px solid var(--line); }}
td {{ padding:4px 6px; font-size:12.5px; border-bottom:1px solid var(--line); }}
.num {{ text-align:right; font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
.desc {{ color:var(--ink); }}
.more {{ color:var(--muted); font-style:italic; font-size:11.5px; }}
.totals td {{ border:none; padding:3px 6px; font-size:13px; }}
.totals .calc {{ color:var(--block); font-weight:700; }}
.totals .grand td {{ border-top:1.5px solid var(--line); padding-top:7px;
  font-weight:700; }}
.actions {{ display:flex; gap:8px; margin-top:13px; flex-wrap:wrap; }}
button {{ font:inherit; font-size:12.5px; padding:7px 13px; border-radius:7px;
  border:1px solid var(--line); background:var(--panel); color:var(--ink);
  cursor:pointer; }}
button.approve {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
button:hover {{ filter:brightness(1.08); }}
.posted {{ background:var(--panel); border:1px solid var(--line);
  border-radius:11px; overflow-x:auto; }}
.posted table {{ margin:0; min-width:560px; }}
.posted th, .posted td {{ padding:8px 14px; }}
.posted td.note {{ color:var(--muted); font-size:12px; }}
.degraded {{ background:#3a2a12; border:1px solid #6b4a1c; color:#e8c98a;
  padding:10px 14px; border-radius:6px; margin:18px 0 0; font-size:13px; }}
.acc {{ color:var(--ok); }}
footer {{ color:var(--muted); font-size:11.5px; margin-top:30px;
  border-top:1px solid var(--line); padding-top:14px; }}
@media (max-width:820px) {{
  .split {{ grid-template-columns:1fr; }}
  .page {{ border-right:none; border-bottom:1px solid var(--line); }}
}}
</style></head><body>
<div class="wrap">
<header>
  <h1>Accounts Payable — Review Queue</h1>
  <div class="sub">{html.escape(str(buyer))} · {summary["documents"]} documents processed ·
    extractor: {html.escape(str(summary["extractor"]))}</div>
</header>

<div class="stats">
  <div class="stat ok"><div class="n">{summary["posted"]}</div><div class="l">Posted</div></div>
  <div class="stat hold"><div class="n">{summary["held_for_review"]}</div><div class="l">Held</div></div>
  {truth_stats}
</div>

{degraded}
<h3 class="sec">Held for review — {len(held)} document{"s" if len(held) != 1 else ""}</h3>
{"".join(cards) if cards else '<div class="card"><div class="detail">Nothing held.</div></div>'}

<h3 class="sec">Posted automatically — {len(posted)} documents</h3>
<div class="posted"><table>
<thead><tr>{posted_head}</tr></thead>
<tbody>{posted_rows}</tbody></table></div>

<footer>
{footer_note}
Held documents are those where a deterministic rule found the record either
internally inconsistent or unsafe to post unattended — not documents the
extractor was merely unsure about.
</footer>
</div></body></html>'''

    Path(out_path).write_text(doc, newline="\n", encoding="utf-8")
    return out_path
