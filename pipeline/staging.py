#!/usr/bin/env python3
"""
Build the staging table: the human step between reading invoices and writing
them into the accounting system.

    python3 pipeline/staging.py --results "<...>/out/results.json" \
        --vendors "<...>/APPROVED_VENDORS.csv" \
        --pdf-dir "<...>/invoices" \
        --client "Brightwater Provisions Co." \
        --out "<...>/out/staging.html"

The review queue this replaces was a report: it told you what happened after
it had already happened. This is a worksheet. Every extracted field is
editable, every rule re-runs as you type, and nothing reaches the ledger until
somebody presses Import.

Three design points worth stating, because they are the ones that were argued
over rather than assumed:

  Clean rows arrive already checked for import. If a reviewer has to tick
  fifty-five boxes, the tool has added work rather than removed it. The saving
  comes from reviewing the exceptions and glancing past the rest.

  Shading is per CELL, not per row. A row with three problems tells you
  nothing you did not already know from the fact that it is held. A shaded
  subtotal tells you where to look. Intensity carries severity; every shaded
  cell also carries a non-colour marker, because colour alone does not survive
  printing or colour blindness.

  Import / Hold / Reject, not Import / Hold. Without a terminal "no", held
  rows accumulate forever and a batch can never reach zero. Reject carries a
  reason, and the reason is the durable fact -- it is what a persistent ledger
  would need in order to stop asking the same question next month.

The rules are evaluated twice on purpose. Python computes them when the run is
scored; the page recomputes them in the browser so that editing a field
re-validates live. Two implementations of one ruleset is a real hazard, so the
thresholds are injected as data rather than restated, and the page compares its
own findings against Python's on load and says so loudly if they disagree.
"""

import argparse
import base64
import csv
import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

# Which field each finding implicates, so the shading lands on the cell a
# reviewer needs to look at rather than smearing across the row.
FIELD_OF = {
    "LINE_SUM_MISMATCH": "subtotal",
    "TOTAL_MISMATCH": "total",
    "LINE_EXTENSION_MISMATCH": "lines",
    "NO_LINE_ITEMS": "lines",
    "UNKNOWN_VENDOR": "vendor_name",
    "VENDOR_NAME_VARIANT": "vendor_name",
    "MISSING_VENDOR": "vendor_name",
    "MISSING_INVOICE_NO": "invoice_number",
    "DUPLICATE_INVOICE_NO": "invoice_number",
    "STALE_DATE": "invoice_date",
    "FUTURE_DATE": "invoice_date",
    "UNPARSEABLE_DATE": "invoice_date",
    "MISSING_DATE": "invoice_date",
    "LARGE_AMOUNT": "total",
}

REJECT_REASONS = [
    "Duplicate of an invoice already entered",
    "Not our invoice - billed to another company",
    "Vendor not approved",
    "Amounts disputed with vendor",
    "Superseded by a corrected invoice",
    "Other (see note)",
]


def page_png(pdf_path: Path, dpi: int) -> str:
    """First page as a data URI, or "" for any failure. Never raises."""
    if not shutil.which("pdftoppm") or not Path(pdf_path).is_file():
        return ""
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                 str(pdf_path), str(Path(td) / "pg")],
                check=True, capture_output=True, timeout=30)
            pngs = sorted(Path(td).glob("pg*.png"))
            if not pngs:
                return ""
            return "data:image/png;base64," + base64.b64encode(
                pngs[0].read_bytes()).decode()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""


def load_vendors(path):
    if not path:
        return []
    rows = list(csv.DictReader(
        Path(path).read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        return []
    col = "name" if "name" in rows[0] else list(rows[0])[0]
    return [r[col].strip() for r in rows if r.get(col, "").strip()]


def build(results_path, out_path, client, vendors_path=None, pdf_dir=None,
          dpi=90, images=True, ledger_path=None, batch_id=None):
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    docs = data["documents"]
    summary = data.get("summary", {})
    approved = load_vendors(vendors_path)
    pdf_dir = Path(pdf_dir) if pdf_dir else None

    batch_id = batch_id or f"B-{date.today().isoformat()}-{Path(results_path).parent.parent.name[:12]}"

    # Cross-batch findings come from the ledger, not from the ruleset. They are
    # kept in their own field rather than mixed into `findings` because the page
    # recomputes findings on every keystroke and cannot recompute these -- it
    # has no database. Keeping them apart also keeps the Python/JS agreement
    # check comparing like with like.
    led = None
    if ledger_path:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ledger import Ledger, file_hash
        import profiles as vprofiles
        led = Ledger(ledger_path)
        VPROF = vprofiles.build(led, client)

    rows, no_image = [], 0
    for d in docs:
        ex = d["extracted"]
        prior = []
        if led:
            prior = led.check(client, ex, exclude_batch=batch_id,
                              content_hash=file_hash(pdf_dir / d["file"])
                              if pdf_dir else None)
        # What this vendor usually does. Separate from both the ruleset (which
        # the page recomputes live) and the duplicate check (which asks "seen
        # before"); this one asks "does this look like them".
        learned = []
        if led:
            learned = vprofiles.check(
                ex, VPROF.get(ex.get("vendor_name_resolved")
                              or ex.get("vendor_name")))
        blocked = (any(f["severity"] == "BLOCK" for f in d["findings"])
                   or any(m["severity"] == "BLOCK" for m in prior)
                   or any(m["severity"] == "BLOCK" for m in learned))
        img = ""
        if images and pdf_dir and blocked:
            img = page_png(pdf_dir / d["file"], dpi)
            if not img:
                no_image += 1
        rows.append({
            "file": d["file"],
            "extracted": ex,
            "py_findings": d["findings"],
            "py_disposition": d["disposition"],
            "prior": prior,
            "learned": learned,
            "image": img,
        })

    payload = {
        "batch_id": batch_id,
        "client": client,
        "source": summary.get("input", ""),
        "generated": date.today().isoformat(),
        "rows": rows,
        "config": {
            "today": date.today().isoformat(),
            "cent": 0.01,
            "half_cent_per_unit": 0.005,
            "stale_after_days": 365,
            "future_grace_days": 1,
            "large_amount": 25000,
            "approved_vendors": approved,
            "field_of": FIELD_OF,
            "reject_reasons": REJECT_REASONS,
        },
        "ledger": {
            "attached": bool(led),
            "path": str(ledger_path) if ledger_path else "",
            "summary": led.summary(client) if led else None,
        },
        "renderer": {
            "images_requested": bool(images and pdf_dir),
            "poppler": bool(shutil.which("pdftoppm")),
            "missing": no_image,
        },
    }

    if led:
        led.close()

    page = TEMPLATE.replace("/*__DATA__*/null",
                            json.dumps(payload, separators=(",", ":")))
    page = page.replace("__CLIENT__", html.escape(client))
    Path(out_path).write_text(page, encoding="utf-8", newline="\n")
    return payload


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice staging — __CLIENT__</title>
<style>
:root{--bg:#14161a;--panel:#1b1e24;--panel2:#222630;--line:#2e3440;
 --ink:#e6e9ef;--muted:#9aa3b2;--accent:#6ea8fe;
 --block:#7f2f2f;--block-bg:#3a1f22;--warn:#7a5a1e;--warn-bg:#332a16;
 --ok:#2f7f5b;--reject:#5a2a52;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 4px;font-size:17px;font-weight:600}
.meta{color:var(--muted);font-size:12px}
.meta b{color:var(--ink);font-weight:600}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
 padding:12px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.chip{border:1px solid var(--line);background:var(--panel2);color:var(--muted);
 padding:4px 10px;border-radius:99px;font-size:12px;cursor:pointer}
.chip.on{border-color:var(--accent);color:var(--ink)}
.chip .n{opacity:.65;margin-left:5px}
.spacer{flex:1}
button{font:inherit;border-radius:6px;border:1px solid var(--line);
 background:var(--panel2);color:var(--ink);padding:7px 14px;cursor:pointer}
button.primary{background:#1d4ed8;border-color:#1d4ed8;font-weight:600}
button.primary:disabled{background:#2a2f3a;border-color:var(--line);
 color:var(--muted);cursor:not-allowed;font-weight:400}
.notice{margin:14px 22px 0;padding:10px 14px;border-radius:6px;font-size:13px;
 border:1px solid var(--warn);background:var(--warn-bg);color:#e8c98a}
.notice.bad{border-color:var(--block);background:var(--block-bg);color:#f0b2b2}
.wrap{padding:14px 22px 60px;overflow-x:auto}
#tbl{border-collapse:collapse;width:100%;min-width:1180px;font-size:13px}
#tbl>thead th{position:sticky;top:0;z-index:2;text-align:left;font-weight:600;
 color:var(--muted);background:var(--panel);border-bottom:1px solid var(--line);
 padding:8px 9px;white-space:nowrap}
#tbl>tbody>tr>td{border-bottom:1px solid var(--line);padding:5px 9px;vertical-align:middle}
tr.row:hover td{background:#1a1d23}
tr.hidden{display:none}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.f{width:100%;background:transparent;border:1px solid transparent;color:inherit;
 font:inherit;padding:3px 5px;border-radius:4px}
.f:hover{border-color:var(--line)}
.f:focus{outline:none;border-color:var(--accent);background:#10131a}
.f.num{text-align:right;font-variant-numeric:tabular-nums}
td.flag-BLOCK{background:var(--block-bg);box-shadow:inset 2px 0 0 var(--block)}
td.flag-WARN{background:var(--warn-bg);box-shadow:inset 2px 0 0 var(--warn)}
.mark{font-size:10px;letter-spacing:.06em;font-weight:700;margin-right:5px;
 opacity:.85}
td.flag-BLOCK .mark{color:#f0a0a0}
td.flag-WARN .mark{color:#e8c98a}
.edited{border-bottom:2px dotted var(--accent) !important}
.disp{display:flex;gap:3px}
.disp label{border:1px solid var(--line);border-radius:5px;padding:3px 8px;
 font-size:11px;cursor:pointer;color:var(--muted);user-select:none}
.disp input{display:none}
.disp input:checked+span{color:var(--ink)}
.disp label:has(input[value=import]:checked){border-color:var(--ok);background:#12281f}
.disp label:has(input[value=hold]:checked){border-color:var(--warn);background:var(--warn-bg)}
.disp label:has(input[value=reject]:checked){border-color:var(--reject);background:#2c1628}
.caret{cursor:pointer;color:var(--muted);user-select:none;padding:0 4px}
tr.detail td{background:#101318;padding:0}
.detail-in{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,400px);
 gap:18px;padding:14px 16px;align-items:start}
/* Grid children default to min-width:auto, which lets a wide line-item table
   refuse to shrink and slide underneath the page image. */
.detail-in>div{min-width:0}
.page{position:sticky;top:46px}
.lines{width:100%;font-size:12px;border-collapse:collapse;table-layout:fixed}
.lines td:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lines th{background:transparent;border-bottom:1px solid var(--line);
 padding:4px 6px;font-size:11px;color:var(--muted);text-align:left}
.lines td{border-bottom:1px dotted var(--line);padding:2px 6px}
.page img{width:100%;border-radius:5px;border:1px solid var(--line);display:block}
.page .none{color:var(--muted);font-size:12px;border:1px dashed var(--line);
 border-radius:5px;padding:24px;text-align:center}
.why{margin-top:10px;font-size:12px;color:var(--muted)}
.why div{padding:3px 0;border-top:1px dotted var(--line)}
.why code{color:#f0b2b2}
.prior{margin:0 0 10px;padding:8px 11px;border-radius:5px;font-size:12px;
 border:1px solid var(--warn);background:var(--warn-bg);color:#e8c98a}
.prior.BLOCK{border-color:var(--block);background:var(--block-bg);color:#f0b2b2}
.prior span{opacity:.85}
.rej{margin-top:8px;display:none}
tr.detail.show-rej .rej{display:block}
select,textarea{font:inherit;background:var(--panel2);color:var(--ink);
 border:1px solid var(--line);border-radius:5px;padding:5px 7px;width:100%}
.log{margin:0 22px 40px;border:1px solid var(--line);border-radius:7px;
 background:var(--panel);font-size:12px}
.log h2{margin:0;padding:10px 14px;font-size:13px;border-bottom:1px solid var(--line)}
.log .body{max-height:220px;overflow:auto;padding:6px 14px}
.log .e{padding:4px 0;border-bottom:1px dotted var(--line);color:var(--muted)}
.log .e b{color:var(--ink);font-weight:600}
.log .none{color:var(--muted);padding:10px 0}
footer{position:fixed;left:0;right:0;bottom:0;display:flex;gap:12px;
 align-items:center;padding:10px 22px;background:var(--panel);
 border-top:1px solid var(--line);font-size:13px}
.tally b{font-variant-numeric:tabular-nums}
</style></head><body>
<header>
  <h1>Invoice staging — __CLIENT__</h1>
  <div class="meta" id="hdr"></div>
</header>
<div class="bar" id="filters"></div>
<div id="notices"></div>
<div class="wrap"><table id="tbl">
  <thead><tr>
    <th style="width:26px"></th><th style="width:88px">File</th>
    <th style="min-width:230px">Vendor</th><th style="width:120px">Invoice no.</th>
    <th style="width:100px">Date</th><th class="num">Subtotal</th><th class="num">Tax</th>
    <th class="num">Freight</th><th class="num">Total</th>
    <th style="width:210px">Disposition</th>
  </tr></thead><tbody id="body"></tbody>
</table></div>
<div class="log"><h2>Change log — every edit, kept</h2>
  <div class="body" id="log"><div class="none">No edits yet.</div></div></div>
<footer>
  <span class="tally" id="tally"></span><span class="spacer" style="flex:1"></span>
  <button id="btn" class="primary" disabled>Import</button>
</footer>
<script>
const DATA = /*__DATA__*/null;
const CFG = DATA.config, R = DATA.rows;
const $ = (s,r) => (r||document).querySelector(s);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money = v => { const n=Number(v); return isFinite(n)
  ? n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : ""; };
const num = v => { const n = Number(String(v==null?"":v).replace(/[,$\s]/g,""));
  return isFinite(n) ? n : NaN; };
const r2 = n => Math.round(n*100)/100;

/* ---- vendor resolution: a direct mirror of pipeline/validation.py. The
   thresholds and the master list arrive as data; only the shape is restated. */
const SUFFIX = /\b(inc|incorporated|llc|l l c|ltd|limited|co|corp|corporation|company|plc|lp|llp)\b/g;
function normVendor(s){
  s = String(s||"").toLowerCase().replace(/&/g," and ");
  s = s.replace(/[^\w\s]/g," ").replace(/\s+/g," ").trim();
  s = s.replace(/^the\s+/,"");
  return s.replace(SUFFIX,"").replace(/\s+/g," ").trim();
}
const VX = (() => {
  const exact=new Set(CFG.approved_vendors), norm=new Map(), squash=new Map();
  for(const n of CFG.approved_vendors){
    const k=normVendor(n);
    if(!norm.has(k)) norm.set(k,n);
    const q=k.replace(/ /g,"");
    if(!squash.has(q)) squash.set(q,n);
  }
  return {exact,norm,squash,squashUsable:squash.size===new Set(norm.values()).size};
})();
function resolveVendor(raw){
  raw=String(raw||"").trim();
  if(!raw) return [null,null];
  if(VX.exact.has(raw)) return [raw,"exact"];
  const n=normVendor(raw);
  if(VX.norm.has(n)) return [VX.norm.get(n),"normalized"];
  if(VX.squashUsable && VX.squash.has(n.replace(/ /g,"")))
    return [VX.squash.get(n.replace(/ /g,"")),"spacing"];
  return [null,null];
}

function daysBetween(a,b){ return Math.round((a-b)/86400000); }

/* ---- the ruleset, recomputed on every keystroke ------------------------ */
function validate(ex, seen){
  const F=[], add=(code,sev,msg,det)=>F.push({code,severity:sev,message:msg,detail:det||""});
  const items = ex.line_items||[];
  const sub=num(ex.subtotal)||0, tax=num(ex.tax)||0,
        fr=num(ex.freight)||0, tot=num(ex.total)||0;

  if(items.length){
    const ls = r2(items.reduce((a,i)=>a+(num(i.amount)||0),0));
    if(Math.abs(ls-sub) > CFG.cent)
      add("LINE_SUM_MISMATCH","BLOCK","Line items do not sum to the stated subtotal",
          `lines total ${money(ls)}, subtotal says ${money(sub)} (off by ${money(Math.abs(ls-sub))})`);
    const bad=[];
    items.forEach((i,n)=>{
      const q=num(i.quantity), p=num(i.unit_price), a=num(i.amount);
      if(!isFinite(q)||!isFinite(p)||!isFinite(a)) return;
      const ext=r2(q*p), gap=Math.abs(ext-a);
      const tol=Math.max(CFG.cent, Math.abs(q)*CFG.half_cent_per_unit);
      if(gap>tol) bad.push(`line ${n+1}: ${q} x ${p} = ${money(ext)} but the line reads ${money(a)}`);
    });
    if(bad.length) add("LINE_EXTENSION_MISMATCH","BLOCK",
      "A line's quantity times unit price does not equal its amount"
      + (bad.length>1?` (${bad.length} lines)`:""), bad.join("; "));
  } else {
    add("NO_LINE_ITEMS","WARN","No line items were extracted","");
  }

  if(Math.abs(r2(sub+tax+fr)-tot) > CFG.cent)
    add("TOTAL_MISMATCH","BLOCK","Subtotal plus tax and freight does not equal the stated total",
        `${money(sub)} + ${money(tax)} + ${money(fr)} = ${money(r2(sub+tax+fr))}, total says ${money(tot)}`);

  const vraw=String(ex.vendor_name||"").trim();
  let resolved=vraw;
  if(!vraw){ resolved=""; add("MISSING_VENDOR","BLOCK","No vendor name could be read",""); }
  else{
    const [canon,tier]=resolveVendor(vraw);
    if(canon===null) add("UNKNOWN_VENDOR","BLOCK","Vendor is not in the vendor master list",
        `read as '${vraw}' — a new vendor, a misread name, or a document that should not be here`);
    else { resolved=canon;
      if(tier!=="exact") add("VENDOR_NAME_VARIANT","WARN",
        "Vendor name is printed differently from the master list",
        `read as '${vraw}', posting against '${canon}' (matched on ${tier})`); }
  }
  ex.vendor_name_resolved=resolved;

  const inv=String(ex.invoice_number||"").trim();
  if(!inv) add("MISSING_INVOICE_NO","BLOCK","No invoice number found",
      "nothing to duplicate-check against");
  else{
    const key=resolved.toLowerCase()+" ||| "+inv.toLowerCase();
    if(seen.has(key)) add("DUPLICATE_INVOICE_NO","BLOCK",
      "This vendor and invoice number appear twice in this batch",
      `also on ${seen.get(key)} — importing both would pay the vendor twice`);
    else seen.set(key, ex.__file);
  }

  const d=ex.invoice_date ? new Date(ex.invoice_date+"T00:00:00") : null;
  if(!ex.invoice_date) add("MISSING_DATE","BLOCK","No invoice date found","");
  else if(isNaN(d)) add("UNPARSEABLE_DATE","BLOCK","Invoice date could not be interpreted",
      `read as '${ex.invoice_date}'`);
  else{
    const age=daysBetween(new Date(CFG.today+"T00:00:00"), d);
    if(age>CFG.stale_after_days) add("STALE_DATE","BLOCK","Invoice is more than a year old",
        `dated ${ex.invoice_date}, ${age} days ago`);
    if(age < -CFG.future_grace_days) add("FUTURE_DATE","BLOCK","Invoice is dated in the future",
        `dated ${ex.invoice_date}, ${Math.abs(age)} days ahead`);
  }
  if(tot>CFG.large_amount) add("LARGE_AMOUNT","WARN",
      "Invoice exceeds the unattended-posting threshold", money(tot));
  return F;
}

function revalidateAll(){
  const seen=new Map();
  for(const r of R){ r.extracted.__file=r.file; r.findings=validate(r.extracted,seen); }
}

/* ---- state ------------------------------------------------------------- */
const LOG=[];
R.forEach(r=>{ r.original=JSON.parse(JSON.stringify(r.extracted)); r.disposition=null;
               r.reason=""; r.note=""; });
revalidateAll();
// First pass sets the default: anything blocked starts on Hold, the rest on
// Import. This is the whole ergonomic argument -- the reviewer works the
// exceptions, not the batch.
const blockedNow = r => r.findings.some(f=>f.severity==="BLOCK")
                     || (r.prior||[]).some(m=>m.severity==="BLOCK")
                     || (r.learned||[]).some(m=>m.severity==="BLOCK");
R.forEach(r=>{ r.disposition = blockedNow(r) ? "hold" : "import"; });

const PRIOR_FIELD={content:"invoice_number",identifier:"invoice_number",
                   "near-miss":"invoice_number",restatement:"total"};
const LEARNED_FIELD={IDENTIFIER_FORMAT:"invoice_number",
  IDENTIFIER_RANGE:"invoice_number",TAX_RATE_UNUSUAL:"tax",
  CURRENCY_CHANGE:"total",AMOUNT_UNUSUAL:"total"};
function fieldFlag(r, field){
  let sev=null;
  for(const f of r.findings){
    if(CFG.field_of[f.code]!==field) continue;
    if(f.severity==="BLOCK") return "BLOCK";
    sev="WARN";
  }
  // Ledger matches are not recomputable in the browser, but they still have to
  // colour the cell a reviewer needs to look at.
  for(const m of (r.prior||[])){
    if(PRIOR_FIELD[m.kind]!==field) continue;
    if(m.severity==="BLOCK") return "BLOCK";
    sev = sev || "WARN";
  }
  for(const m of (r.learned||[])){
    if(LEARNED_FIELD[m.code]!==field) continue;
    if(m.severity==="BLOCK") return "BLOCK";
    sev = sev || "WARN";
  }
  return sev;
}
const MARK={BLOCK:"!!",WARN:"!"};

/* ---- render ------------------------------------------------------------ */
function cell(r,field,cls){
  const sev=fieldFlag(r,field);
  const edited = String(r.extracted[field]??"")!==String(r.original[field]??"");
  const isNum=/subtotal|tax|freight|total/.test(field);
  const val = isNum ? money(r.extracted[field]) : (r.extracted[field]??"");
  return `<td class="${cls||""}${sev?" flag-"+sev:""}">`
    + (sev?`<span class="mark">${MARK[sev]}</span>`:"")
    + `<input class="f ${isNum?"num":""}${edited?" edited":""}" data-f="${field}"
        value="${esc(val)}" style="width:${sev?"calc(100% - 22px)":"100%"}">`
    + `</td>`;
}

function rowHtml(r,i){
  const d=r.disposition;
  const radio=(v,label)=>`<label><input type="radio" name="d${i}" value="${v}"
      ${d===v?"checked":""}><span>${label}</span></label>`;
  return `<tr class="row" data-i="${i}">
    <td><span class="caret" data-x="${i}">&#9656;</span></td>
    <td style="white-space:nowrap">${esc(r.file)}</td>
    ${cell(r,"vendor_name")}${cell(r,"invoice_number")}${cell(r,"invoice_date")}
    ${cell(r,"subtotal","num")}${cell(r,"tax","num")}${cell(r,"freight","num")}${cell(r,"total","num")}
    <td><div class="disp">${radio("import","Import")}${radio("hold","Hold")}${radio("reject","Reject")}</div></td>
  </tr>
  <tr class="detail hidden ${d==="reject"?"show-rej":""}" data-d="${i}"><td colspan="10">
    <div class="detail-in">
      <div>
        <table class="lines"><thead><tr><th>Description</th><th class="num">Qty</th>
          <th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
          <tbody>${(r.extracted.line_items||[]).map((li,n)=>`<tr>
            <td>${esc(li.description)}</td>
            <td class="num"><input class="f num" data-li="${n}" data-k="quantity" value="${esc(li.quantity)}"></td>
            <td class="num"><input class="f num" data-li="${n}" data-k="unit_price" value="${esc(li.unit_price)}"></td>
            <td class="num"><input class="f num" data-li="${n}" data-k="amount" value="${esc(li.amount)}"></td>
          </tr>`).join("")}</tbody></table>
        ${(r.learned||[]).map(m=>`<div class="prior ${m.severity}">
           <b>Unlike this vendor — ${esc(m.code)}</b><br>
           <span>${esc(m.detail)}</span></div>`).join("")}
        ${(r.prior||[]).map(m=>`<div class="prior ${m.severity}">
           <b>Seen before — ${esc(m.kind)}</b> · ${esc(m.why)}<br>
           <span>${esc(m.file)} in batch ${esc(m.batch_id)} — ${esc(m.vendor)},
           ${esc(m.invoice_date)}, ${esc(m.total)}, invoice ${esc(m.invoice_number)}
           (imported ${esc((m.seen_at||"").slice(0,10))})</span></div>`).join("")}
        <div class="why">${r.findings.map(f=>`<div><code>${f.code}</code> —
           ${esc(f.message)}${f.detail?`<br><span style="opacity:.8">${esc(f.detail)}</span>`:""}</div>`).join("")
           || `<div style="border:0">No findings.</div>`}</div>
        <div class="rej">
          <select data-rej="${i}">${CFG.reject_reasons.map(x=>
            `<option${r.reason===x?" selected":""}>${esc(x)}</option>`).join("")}</select>
          <textarea data-note="${i}" rows="2" placeholder="Note (kept with the rejection)"
            style="margin-top:6px">${esc(r.note)}</textarea>
        </div>
      </div>
      <div class="page">${r.image?`<img loading="lazy" src="${r.image}" alt="page 1">`
        :`<div class="none">No page image</div>`}</div>
    </div>
  </td></tr>`;
}

let filter=null;
function render(){
  $("#body").innerHTML = R.map(rowHtml).join("");
  applyFilter();
  renderFilters(); renderTally(); renderLog();
}

function renderFilters(){
  const counts={};
  R.forEach(r=>r.findings.forEach(f=>counts[f.code]=(counts[f.code]||0)+1));
  const codes=Object.keys(counts).sort();
  $("#filters").innerHTML =
    `<span class="chip${filter===null?" on":""}" data-c="">All<span class="n">${R.length}</span></span>`
    + codes.map(c=>`<span class="chip${filter===c?" on":""}" data-c="${c}">${c}<span class="n">${counts[c]}</span></span>`).join("")
    + `<span class="spacer"></span>`
    + `<button id="expandAll">Expand flagged</button>`;
}
function applyFilter(){
  document.querySelectorAll("tr.row").forEach(tr=>{
    const r=R[+tr.dataset.i];
    const show = !filter || r.findings.some(f=>f.code===filter);
    tr.classList.toggle("hidden",!show);
    const det=$(`tr.detail[data-d="${tr.dataset.i}"]`);
    if(!show) det.classList.add("hidden");
  });
}
function renderTally(){
  const c={import:0,hold:0,reject:0};
  R.forEach(r=>c[r.disposition]++);
  $("#tally").innerHTML=`<b>${c.import}</b> to import &nbsp;·&nbsp; <b>${c.hold}</b> held
    &nbsp;·&nbsp; <b>${c.reject}</b> rejected &nbsp;·&nbsp;
    <span style="color:var(--muted)">${R.length} in batch</span>`;
  const btn=$("#btn");
  btn.disabled=!c.import;
  btn.textContent=c.import?`Import ${c.import} invoice${c.import===1?"":"s"}`:"Import";
}
function renderLog(){
  const el=$("#log");
  if(!LOG.length){ el.innerHTML=`<div class="none">No edits yet.</div>`; return; }
  el.innerHTML=LOG.slice().reverse().map(e=>`<div class="e">
    <b>${esc(e.file)}</b> · ${esc(e.field)} · <span style="color:#f0b2b2">${esc(e.from)}</span>
    &rarr; <span style="color:#9ae6b4">${esc(e.to)}</span>
    <span style="float:right">${e.at} · ${esc(e.who)}</span></div>`).join("");
}

function recordEdit(file,field,from,to){
  LOG.push({file,field,from:String(from??""),to:String(to??""),
    at:new Date().toLocaleTimeString(),who:"reviewer"});
}

/* ---- events ------------------------------------------------------------ */
$("#body").addEventListener("change", e=>{
  const t=e.target;
  if(t.classList.contains("f")){
    const tr=t.closest("tr"); const i=+(tr.dataset.i ?? tr.dataset.d);
    const r=R[i];
    if(t.dataset.li!==undefined){
      const li=r.extracted.line_items[+t.dataset.li], k=t.dataset.k;
      if(String(li[k])!==t.value){ recordEdit(r.file,`line ${+t.dataset.li+1} ${k}`,li[k],t.value); li[k]=t.value; }
    } else {
      const f=t.dataset.f, was=r.extracted[f];
      const v=/subtotal|tax|freight|total/.test(f) ? String(num(t.value)) : t.value;
      if(String(was)!==v){ recordEdit(r.file,f,was,v); r.extracted[f]=v; }
    }
    const wasBlocked = blockedNow(r);
    revalidateAll();
    const nowBlocked = blockedNow(r);
    if(r.disposition!=="reject" && wasBlocked!==nowBlocked)
      r.disposition = nowBlocked ? "hold" : "import";
    render(); return;
  }
  if(t.type==="radio"){
    const i=+t.closest("tr").dataset.i; R[i].disposition=t.value;
    $(`tr.detail[data-d="${i}"]`).classList.toggle("show-rej",t.value==="reject");
    if(t.value==="reject" && !R[i].reason) R[i].reason=CFG.reject_reasons[0];
    renderTally(); return;
  }
  if(t.dataset.rej!==undefined){ R[+t.dataset.rej].reason=t.value; }
  if(t.dataset.note!==undefined){ R[+t.dataset.note].note=t.value; }
});
$("#body").addEventListener("click", e=>{
  const c=e.target.closest(".caret"); if(!c) return;
  $(`tr.detail[data-d="${c.dataset.x}"]`).classList.toggle("hidden");
});
$("#filters").addEventListener("click", e=>{
  const chip=e.target.closest(".chip");
  if(chip){ filter=chip.dataset.c||null; renderFilters(); applyFilter(); return; }
  if(e.target.id==="expandAll"){
    R.forEach((r,i)=>{ if(r.findings.length)
      $(`tr.detail[data-d="${i}"]`).classList.remove("hidden"); });
  }
});
$("#btn").addEventListener("click", ()=>{
  const go=R.filter(r=>r.disposition==="import");
  alert(`Prototype — no accounting system is connected.\n\n`
    + `${go.length} invoices would be written, under their resolved vendor names.\n`
    + `${R.filter(r=>r.disposition==="hold").length} stay held; the batch is not closed `
    + `until every row is imported or rejected.\n\n`
    + `${LOG.length} field edits would be written to the change log alongside them.`);
});

/* ---- header, and the two things this page is honest about -------------- */
$("#hdr").innerHTML = `Batch <b>${esc(DATA.batch_id)}</b> · generated ${DATA.generated}
  · <b>${R.length}</b> documents · source <span style="opacity:.8">${esc(DATA.source||"—")}</span>`;

const notices=[];
// 1. The port check. Two implementations of one ruleset is a real hazard, so
//    the page proves it agrees with Python before anyone trusts a cell colour.
(function(){
  let diff=0;
  R.forEach(r=>{
    const a=r.py_findings.map(f=>f.code).sort().join(",");
    const b=r.findings.map(f=>f.code).sort().join(",");
    if(a!==b) diff++;
  });
  if(diff) notices.push([`bad`,`<b>Rule mismatch on ${diff} document${diff===1?"":"s"}.</b>
    The rules in this page disagree with the ones that produced results.json.
    The shading below cannot be trusted until they are reconciled.`]);
})();
// 2. The gap this prototype cannot close on its own.
(function(){
  const dupes=R.filter(r=>r.findings.some(f=>f.code==="DUPLICATE_INVOICE_NO")).length;
  if(!DATA.ledger || !DATA.ledger.attached){
    notices.push([``,`<b>Duplicate checking covers this batch only.</b>
      ${R.length} invoices were compared against each other and found ${dupes} duplicate${dupes===1?"":"s"}.
      None were compared against anything imported last week or last month, because
      nothing is kept between runs — so a vendor re-sending an invoice in a later
      batch would post unchallenged. That is the case the client described, and
      closing it needs a ledger that outlives the batch.`]);
    return;
  }
  const learned=R.filter(r=>(r.learned||[]).length);
  if(learned.length) notices.push([``,`<b>${learned.length} invoice${learned.length===1?"":"s"}
    ${learned.length===1?"does not":"do not"} look like the vendor they came from.</b>
    Compared against habits learned from invoices already imported — number
    format, numbering range, usual tax rate, currency — not against anything
    anyone configured.`]);
  const s=DATA.ledger.summary||{}, hits=R.filter(r=>(r.prior||[]).length);
  const byKind={};
  hits.forEach(r=>r.prior.forEach(m=>byKind[m.kind]=(byKind[m.kind]||0)+1));
  const kinds=Object.entries(byKind).map(([k,n])=>`${n} ${k}`).join(", ");
  notices.push([``,`<b>Checked against ${s.imported||0} invoices imported in
    ${s.batches||0} earlier batch${(s.batches||0)===1?"":"es"}.</b>
    ${hits.length?`${hits.length} document${hits.length===1?"":"s"} here
      ${hits.length===1?"matches":"match"} something already entered (${kinds}).
      Each is held, with the earlier document named on the row.`
     :`Nothing in this batch matches anything previously entered.`}`]);
})();
if(DATA.renderer.images_requested && (!DATA.renderer.poppler || DATA.renderer.missing))
  notices.push([``,`<b>Page images unavailable.</b> `
    + (!DATA.renderer.poppler
       ? `poppler is not installed, so no invoice pages could be rendered — a reviewer
          is editing figures with nothing to check them against.`
       : `${DATA.renderer.missing} pages could not be rendered.`)]);
$("#notices").innerHTML = notices.map(([k,h])=>`<div class="notice ${k}">${h}</div>`).join("");

render();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--client", default="")
    ap.add_argument("--vendors")
    ap.add_argument("--pdf-dir")
    ap.add_argument("--dpi", type=int, default=90)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--ledger", help="SQLite ledger to check prior batches against")
    ap.add_argument("--batch-id", help="identifier for this batch")
    a = ap.parse_args()

    p = build(a.results, a.out, a.client or "client", a.vendors, a.pdf_dir,
              a.dpi, not a.no_images, a.ledger, a.batch_id)
    size = Path(a.out).stat().st_size
    print(f"\n  batch      {p['batch_id']}")
    print(f"  documents  {len(p['rows'])}")
    print(f"  images     {sum(1 for r in p['rows'] if r['image'])} embedded"
          + (f", {p['renderer']['missing']} unavailable" if p['renderer']['missing'] else ""))
    if p["ledger"]["attached"]:
        n = sum(1 for r in p["rows"] if r["prior"])
        print(f"  ledger     {p['ledger']['summary']}")
        print(f"  prior hits {n} document{'' if n == 1 else 's'} match something "
              f"already imported")
    print(f"  staging    {a.out}  ({size/1e6:.1f} MB)\n")


if __name__ == "__main__":
    main()
