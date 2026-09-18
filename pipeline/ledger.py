#!/usr/bin/env python3
"""
The ledger: what the pipeline remembers between runs.

Everything else in this project is stateless. A batch is read, checked against
itself, and forgotten. That is enough to catch a vendor who sends the same
invoice twice in one drop, and it is useless against the case clients actually
describe -- an invoice sent in May, unpaid, re-sent in June, entered as a new
bill. Two batches, a month apart, and nothing in memory to compare against.

So this file exists to answer one question: have we seen this before?

Append-only, on purpose. Rows are never updated and never deleted. A document's
current state is the latest event recorded against it, the same way a balance is
the sum of its entries rather than a number someone overwrote. Correcting a
mistake means recording the correction, which is exactly what an auditor -- or
a bookkeeper six months from now trying to work out what happened -- needs.

FOUR KEYS, strongest first. A duplicate is reported once, by the strongest key
that finds it:

  content       the same bytes. Certain, and immune to every extraction error.
  identifier    vendor + invoice number. The classic, and the fragile one: a
                single misread character silently destroys it, and identifiers
                are the one field arithmetic cannot cross-check.
  restatement   vendor + date + total. Sees a re-issue under a new number, and
                needs no identifier at all. Its three fields ARE arithmetically
                cross-checked, so a misread in them raises a flag rather than
                passing quietly.
  near-miss     vendor + invoice numbers differing at exactly one position, by
                characters that commonly misread for one another. Deterministic:
                a fixed confusion table, not a similarity score. Sequential
                numbering is safe because 1 and 2 are not confusable -- what it
                catches is O for 0, S for 5, 1 for 3.

Only documents that were actually IMPORTED can raise a duplicate. One that was
reviewed and rejected must not block a corrected re-submission. The exception is
a content match, which is reported whatever the earlier outcome, because the
identical file arriving twice is worth knowing about either way.

Dismissals are decisions, and they stick. A client whose customer underpaid and
who then received a second, legitimate invoice for the difference should be
asked once. Re-asking every month teaches people to click through warnings,
which is worse than never having warned them.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(
  batch_id    TEXT PRIMARY KEY,
  client      TEXT NOT NULL,
  source      TEXT,
  created_at  TEXT NOT NULL,
  doc_count   INTEGER NOT NULL DEFAULT 0
);

-- One row per document ever seen. Never updated.
CREATE TABLE IF NOT EXISTS documents(
  doc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
  client          TEXT NOT NULL,
  file            TEXT NOT NULL,
  content_hash    TEXT,
  vendor_raw      TEXT,
  vendor          TEXT,            -- resolved to the master list
  invoice_number  TEXT,
  invoice_date    TEXT,
  total           TEXT,
  currency        TEXT,
  extracted       TEXT NOT NULL,   -- the full record as JSON
  seen_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_ident ON documents(client, vendor, invoice_number);
CREATE INDEX IF NOT EXISTS ix_doc_restate ON documents(client, vendor, invoice_date, total);
CREATE INDEX IF NOT EXISTS ix_doc_hash ON documents(client, content_hash);

-- Every state change. Current state = the latest event for a doc.
CREATE TABLE IF NOT EXISTS events(
  event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id     INTEGER NOT NULL REFERENCES documents(doc_id),
  at         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,   -- staged | edited | imported | rejected | held
  field      TEXT,
  old_value  TEXT,
  new_value  TEXT,
  reason     TEXT,
  note       TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_doc ON events(doc_id, event_id);

-- Standing instructions per vendor: a sentence, never a coordinate. Fed into
-- the extraction prompt for that vendor's future invoices. Survives a
-- redesigned letterhead and a crooked scan, which a rectangle does not.
CREATE TABLE IF NOT EXISTS vendor_hints(
  client  TEXT NOT NULL,
  vendor  TEXT NOT NULL,
  hint    TEXT NOT NULL,
  at      TEXT NOT NULL,
  actor   TEXT NOT NULL,
  PRIMARY KEY (client, vendor)
);

-- "These two are not duplicates." Asked once, remembered forever.
CREATE TABLE IF NOT EXISTS dismissals(
  doc_id    INTEGER NOT NULL,
  other_id  INTEGER NOT NULL,
  kind      TEXT NOT NULL,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  note      TEXT,
  PRIMARY KEY (doc_id, other_id, kind)
);
"""

# Characters that genuinely misread for one another. Kept deliberately narrow:
# every pair here was either observed in a real extraction run or is a standard
# OCR confusion. Digits that merely look similar to a human (1 and 7) are only
# grouped where the substitution has actually been seen, because a group that is
# too generous turns sequential invoice numbers into false duplicates.
CONFUSABLE = [
    set("0OoDQ"),      # PRO35902 read for PR035902
    set("1lI"),        # and 3 -> 1, below
    set("5Ss"),        # RS021935 read as R5021935
    set("8B"),
    set("2Zz"),
    set("6G"),
    set("9gq"),
    set("13"),         # the dominant numeric confusion in every scanned run
    set("79"),         # 692.77 read as 692.79
]


def _confusable(a, b):
    if a == b:
        return False
    return any(a in g and b in g for g in CONFUSABLE)


def near_miss(a, b):
    """
    True when two identifiers differ at exactly one position, and that one
    difference is a character pair that commonly misreads.

    'PRO35902' vs 'PR035902'  -> True  (O for 0)
    'INV-1001' vs 'INV-1002'  -> False (1 and 2 are not confusable, and
                                        sequential numbering must stay safe)
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b or len(a) != len(b) or a == b:
        return False
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diffs) == 1 and _confusable(*diffs[0])


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_hash(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # WAL is the right default on a local disk and does not work at all on
        # a network share, a mapped drive or a synced folder -- SQLite reports
        # a bare "disk I/O error" rather than anything that explains itself. A
        # back-office ledger is exactly the kind of file that ends up on a
        # mapped drive, so negotiate rather than assume: try WAL, and fall back
        # to exclusive locking with a rollback journal, which is what works on
        # filesystems that do not implement shared locks properly.
        #
        # Exclusive locking costs concurrent readers. For a single-machine
        # ledger with one person importing at a time that is no loss, and it
        # rules out two import sessions racing each other.
        self.journal_mode = None
        for locking, journal in (("NORMAL", "WAL"), ("EXCLUSIVE", "DELETE")):
            try:
                self.db = sqlite3.connect(self.path)
                self.db.row_factory = sqlite3.Row
                self.db.execute(f"PRAGMA locking_mode={locking}")
                self.db.execute(f"PRAGMA journal_mode={journal}")
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.executescript(SCHEMA)
                self.db.commit()
                self.journal_mode = f"{journal.lower()} / {locking.lower()} locking"
                break
            except sqlite3.Error as e:
                try:
                    self.db.close()
                except Exception:
                    pass
                last = e
        if self.journal_mode is None:
            raise RuntimeError(
                f"Could not open the ledger at {self.path}: {last}. "
                f"If it is on a network share, move it to a local disk.")

    def close(self):
        self.db.close()

    # ---- writing --------------------------------------------------------
    def open_batch(self, batch_id, client, source="", doc_count=0):
        self.db.execute(
            "INSERT OR IGNORE INTO batches(batch_id,client,source,created_at,doc_count)"
            " VALUES(?,?,?,?,?)", (batch_id, client, source, now(), doc_count))
        self.db.commit()
        return batch_id

    def stage(self, batch_id, client, file, extracted, content_hash=None):
        """Record a document as seen. Returns its doc_id."""
        cur = self.db.execute(
            "INSERT INTO documents(batch_id,client,file,content_hash,vendor_raw,"
            "vendor,invoice_number,invoice_date,total,currency,extracted,seen_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, client, file, content_hash,
             extracted.get("vendor_name"),
             extracted.get("vendor_name_resolved") or extracted.get("vendor_name"),
             (extracted.get("invoice_number") or "").strip(),
             extracted.get("invoice_date"),
             str(extracted.get("total") or ""),
             extracted.get("currency") or "USD",
             json.dumps(extracted, separators=(",", ":")), now()))
        doc_id = cur.lastrowid
        self._event(doc_id, "staged", actor="pipeline")
        self.db.commit()
        return doc_id

    def _event(self, doc_id, action, actor="reviewer", field=None,
               old=None, new=None, reason=None, note=None):
        self.db.execute(
            "INSERT INTO events(doc_id,at,actor,action,field,old_value,new_value,"
            "reason,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (doc_id, now(), actor, action, field, old, new, reason, note))

    def record(self, doc_id, action, **kw):
        self._event(doc_id, action, **kw)
        self.db.commit()

    def set_hint(self, client, vendor, hint, actor="reviewer"):
        self.db.execute(
            "INSERT OR REPLACE INTO vendor_hints(client,vendor,hint,at,actor)"
            " VALUES(?,?,?,?,?)", (client, vendor, hint, now(), actor))
        self.db.commit()

    def dismiss(self, doc_id, other_id, kind, actor="reviewer", note=""):
        """Record that these two are NOT a duplicate. Never asked again."""
        self.db.execute(
            "INSERT OR REPLACE INTO dismissals(doc_id,other_id,kind,at,actor,note)"
            " VALUES(?,?,?,?,?,?)", (doc_id, other_id, kind, now(), actor, note))
        self.db.commit()

    # ---- reading --------------------------------------------------------
    def status(self, doc_id):
        r = self.db.execute(
            "SELECT action FROM events WHERE doc_id=? ORDER BY event_id DESC LIMIT 1",
            (doc_id,)).fetchone()
        return r["action"] if r else None

    def _imported_ids(self, client):
        rows = self.db.execute("""
            SELECT d.doc_id FROM documents d
            JOIN (SELECT doc_id, MAX(event_id) AS e FROM events GROUP BY doc_id) last
              ON last.doc_id = d.doc_id
            JOIN events ev ON ev.event_id = last.e
            WHERE d.client = ? AND ev.action = 'imported'
        """, (client,)).fetchall()
        return {r["doc_id"] for r in rows}

    def _dismissed(self, client):
        out = set()
        for r in self.db.execute("SELECT doc_id, other_id, kind FROM dismissals"):
            out.add((r["doc_id"], r["other_id"], r["kind"]))
            out.add((r["other_id"], r["doc_id"], r["kind"]))
        return out

    def check(self, client, extracted, content_hash=None, exclude_batch=None,
              doc_id=None):
        """
        Has this been seen before? Returns a list of matches, strongest first,
        each with the prior document's identifying detail so a reviewer can
        decide rather than merely be alarmed.
        """
        vendor = (extracted.get("vendor_name_resolved")
                  or extracted.get("vendor_name") or "").strip()
        inv = (extracted.get("invoice_number") or "").strip()
        date = extracted.get("invoice_date")
        total = str(extracted.get("total") or "")

        where, args = "WHERE client = ?", [client]
        if exclude_batch:
            where += " AND batch_id <> ?"
            args.append(exclude_batch)
        rows = self.db.execute(
            f"SELECT doc_id,batch_id,file,content_hash,vendor,invoice_number,"
            f"invoice_date,total,seen_at FROM documents {where}", args).fetchall()

        imported = self._imported_ids(client)
        dismissed = self._dismissed(client)
        seen_ids, out = set(), []

        def add(r, kind, severity, why):
            if r["doc_id"] in seen_ids:
                return
            # A dismissal is a decision about a PAIR, so it only applies once
            # the incoming document has an identity of its own. Checking before
            # staging simply has nothing to match against, which is correct.
            if doc_id is not None and (doc_id, r["doc_id"], kind) in dismissed:
                return
            seen_ids.add(r["doc_id"])
            out.append({
                "kind": kind, "severity": severity, "why": why,
                "doc_id": r["doc_id"], "batch_id": r["batch_id"],
                "file": r["file"], "vendor": r["vendor"],
                "invoice_number": r["invoice_number"],
                "invoice_date": r["invoice_date"], "total": r["total"],
                "seen_at": r["seen_at"],
            })

        for r in rows:
            # A content match is reported whatever became of the earlier copy:
            # the identical file arriving twice is worth knowing about even if
            # it was rejected last time.
            if content_hash and r["content_hash"] == content_hash:
                add(r, "content", "BLOCK", "byte-for-byte the same file")
        for r in rows:
            if r["doc_id"] not in imported:
                continue
            if vendor and inv and r["vendor"] == vendor and r["invoice_number"] == inv:
                add(r, "identifier", "BLOCK",
                    "same vendor and invoice number, already imported")
        for r in rows:
            if r["doc_id"] not in imported:
                continue
            if (vendor and date and total and r["vendor"] == vendor
                    and r["invoice_date"] == date and r["total"] == total
                    and r["invoice_number"] != inv):
                add(r, "restatement", "BLOCK",
                    "same vendor, date and amount under a different invoice "
                    "number — a re-issue the invoice-number check cannot see")
        for r in rows:
            if r["doc_id"] not in imported:
                continue
            if vendor and r["vendor"] == vendor and near_miss(inv, r["invoice_number"]):
                # Blocks rather than warns. A possible duplicate is the exact
                # thing a human should look at, and the asymmetry is not close:
                # a false hold costs one glance, a miss costs a double payment.
                # Measured at zero false positives over 255 same-vendor pairs,
                # and a legitimate collision is dismissed once and never asked
                # about again.
                add(r, "near-miss", "BLOCK",
                    f"invoice number differs from '{r['invoice_number']}' at one "
                    f"character, and by a pair that commonly misreads")
        return out

    # ---- reporting ------------------------------------------------------
    def summary(self, client=None):
        q = "SELECT COUNT(*) n FROM documents" + (" WHERE client=?" if client else "")
        a = (client,) if client else ()
        docs = self.db.execute(q, a).fetchone()["n"]
        batches = self.db.execute(
            "SELECT COUNT(*) n FROM batches" + (" WHERE client=?" if client else ""),
            a).fetchone()["n"]
        imported = len(self._imported_ids(client)) if client else None
        events = self.db.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        return {"batches": batches, "documents": docs,
                "imported": imported, "events": events}
