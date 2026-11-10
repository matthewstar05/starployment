"""Copy confirmed applications to a Google Sheet the candidate owns.

Starployment never holds a Google account. The candidate pastes ``setup_script()``
into the sheet (Extensions → Apps Script) and deploys it as a web app that runs
as them. A random token, stored only in the local vault, is the only credential.

Sync is opt-in. It sends applications whose status is applied (a confirmed
submission), never deletes rows, and only posts rows that are new or changed.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass

import httpx

from . import db

# The script the candidate pastes into the sheet. ``{{TOKEN}}`` is replaced
# when the script is copied; the token never lives in this source file.
APPS_SCRIPT = r"""/**
 * Starployment writes confirmed applications into this spreadsheet.
 * Deploy: Extensions, Apps Script, Deploy, New deployment, Web app.
 * Execute as: Me. Who has access: Anyone.
 */
var TOKEN = "{{TOKEN}}";

function doPost(e) {
  var body = {};
  try {
    body = JSON.parse((e.postData && e.postData.contents) || "{}");
  } catch (err) {
    return respond_({ok: false, error: "The request was not JSON"});
  }
  if (!body || body.token !== TOKEN) {
    return respond_({ok: false, error: "bad token"});
  }
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
  } catch (err) {
    return respond_({ok: false, error: "The sheet is busy. Try again."});
  }
  try {
    var ss = SpreadsheetApp.getActive();
    var title = ss.getName();
    var url = ss.getUrl();
    if (body.action === "ping") {
      return respond_({ok: true, title: title, url: url});
    }
    var tab = String(body.tab || "Applications");
    var sheet = ss.getSheetByName(tab);
    if (!sheet) {
      sheet = ss.insertSheet(tab);
    }
    var columns = body.columns || [];
    var incoming = body.rows || [];
    var header = readHeader_(sheet);
    if (!header.length) {
      header = columns.map(function(name) { return String(name); });
      if (!header.length) {
        return respond_({ok: false, error: "missing columns", title: title, url: url});
      }
      sheet.getRange(1, 1, 1, header.length).setValues([header]);
    }
    var index = {};
    for (var i = 0; i < header.length; i++) {
      if (header[i]) index[String(header[i])] = i;
    }
    var grew = false;
    for (var c = 0; c < columns.length; c++) {
      var name = String(columns[c]);
      if (index[name] === undefined) {
        header.push(name);
        index[name] = header.length - 1;
        grew = true;
      }
    }
    var idName = "Starployment ID";
    if (index[idName] === undefined) {
      header.push(idName);
      index[idName] = header.length - 1;
      grew = true;
    }
    if (grew) {
      sheet.getRange(1, 1, 1, header.length).setValues([header]);
    }
    var width = header.length;
    var idCol = index[idName];
    var last = sheet.getLastRow();
    var existing = {};
    var data = [];
    var formulas = [];
    var ours = {};
    for (var o = 0; o < columns.length; o++) ours[String(columns[o])] = true;
    ours[idName] = true;
    if (last > 1) {
      var body_ = sheet.getRange(2, 1, last - 1, width);
      data = body_.getValues();
      formulas = body_.getFormulas();
      var ids = sheet.getRange(2, idCol + 1, last - 1, 1).getValues();
      for (var r = 0; r < ids.length; r++) {
        var key = String(ids[r][0] == null ? "" : ids[r][0]);
        if (key) existing[key] = r;
      }
    }
    var updated = 0;
    var appended = 0;
    var appendRows = [];
    var dirty = {};
    for (var n = 0; n < incoming.length; n++) {
      var fields = fieldsOf_(incoming[n], columns);
      var sid = String(fields[idName] == null ? "" : fields[idName]);
      if (sid && existing[sid] !== undefined && existing[sid] >= 0) {
        var at = existing[sid];
        var line = data[at] ? data[at].slice() : [];
        while (line.length < width) line.push("");
        // Rewriting the row would flatten formulas in columns the candidate added.
        for (var f = 0; f < width; f++) {
          if (!ours[header[f]] && formulas[at] && formulas[at][f]) line[f] = formulas[at][f];
        }
        writeFields_(line, fields, index);
        data[at] = line;
        dirty[at] = true;
        updated++;
      } else {
        var blank = [];
        for (var h = 0; h < width; h++) blank.push("");
        writeFields_(blank, fields, index);
        appendRows.push(blank);
        appended++;
        if (sid) existing[sid] = -1;
      }
    }
    var rowIndexes = Object.keys(dirty).map(function(value) { return Number(value); });
    rowIndexes.sort(function(a, b) { return a - b; });
    var blockStart = -1;
    var block = [];
    function flush() {
      if (!block.length) return;
      sheet.getRange(blockStart + 2, 1, block.length, width).setValues(block);
      block = [];
      blockStart = -1;
    }
    for (var d = 0; d < rowIndexes.length; d++) {
      var rowIndex = rowIndexes[d];
      if (blockStart >= 0 && rowIndex === blockStart + block.length) {
        block.push(data[rowIndex]);
      } else {
        flush();
        blockStart = rowIndex;
        block = [data[rowIndex]];
      }
    }
    flush();
    if (appendRows.length) {
      var start = Math.max(sheet.getLastRow(), 1) + 1;
      sheet.getRange(start, 1, appendRows.length, width).setValues(appendRows);
    }
    return respond_({ok: true, updated: updated, appended: appended, title: title, url: url});
  } finally {
    lock.releaseLock();
  }
}

function readHeader_(sheet) {
  if (sheet.getLastRow() < 1 || sheet.getLastColumn() < 1) return [];
  return sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(function(cell) {
    return String(cell == null ? "" : cell);
  });
}

function fieldsOf_(record, columns) {
  var fields = {};
  if (Array.isArray(record)) {
    for (var k = 0; k < columns.length && k < record.length; k++) {
      fields[String(columns[k])] = record[k];
    }
    return fields;
  }
  if (record && typeof record === "object") return record;
  return fields;
}

function writeFields_(line, fields, index) {
  for (var key in fields) {
    if (!Object.prototype.hasOwnProperty.call(fields, key) || index[key] === undefined) continue;
    var value = fields[key];
    line[index[key]] = value == null ? "" : value;
  }
}

function respond_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}
"""

COLUMNS = (
    "Company",
    "Role",
    "Location",
    "Salary",
    "Applied",
    "Stage",
    "Stage updated",
    "Source",
    "Match",
    "Posting URL",
    "Note",
    "Starployment ID",
)
_ID = COLUMNS.index("Starployment ID")
BATCH = 500
_SERVICE = "Starployment Google Sheets"
_ACCOUNT = "token"
_PLACEHOLDER = "{{TOKEN}}"
_WEB_APP = re.compile(r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec")
_FORMULA = "=+-@"
OFFLINE = "Couldn't reach your Google Sheet"
_token_lock = threading.Lock()


class SheetSyncError(RuntimeError):
    """The sheet could not be updated. Network failures start with ``OFFLINE``."""


@dataclass
class SheetSyncReport:
    sent: int
    updated: int
    appended: int
    total: int
    summary: str


def _guard(value: object) -> str:
    """Stop a posting title from becoming a formula when the sheet opens it.

    Only a real first character counts. An empty string is a substring of every
    string, so ``text[:1] in "=+-@"`` would mark blank cells as formulas.
    """
    text = "" if value is None else str(value)
    if text and text[0] in _FORMULA:
        return "'" + text
    return text


def _salary(minimum: int | None, maximum: int | None) -> str:
    """Compact salary, matching the Tracker. Blank when the posting lists none."""
    def compact(amount: int) -> str:
        return f"${amount / 1000:,.0f}k" if amount >= 1000 else f"${amount:,}"

    if minimum and maximum:
        return compact(minimum) if minimum == maximum else f"{compact(minimum)}\u2013{compact(maximum)}"
    if minimum:
        return f"{compact(minimum)}+"
    if maximum:
        return f"Up to {compact(maximum)}"
    return ""


def _source(record: dict) -> str:
    if record.get("method") == "manual":
        return "manual"
    return str(record.get("ats") or record.get("source") or "")


def _posting_url(record: dict) -> str:
    url = str(record.get("url") or "")
    if url.startswith("manual://"):
        return ""
    return url


def rows() -> list[list[str]]:
    """Confirmed tracker rows (status applied), in the sheet's column order."""
    out = []
    for record in db.list_tracker(limit=1_000_000):
        if record.get("status") != "applied":
            continue
        values = [
            record.get("company") or "",
            record.get("title") or "",
            record.get("location") or "",
            _salary(record.get("salary_min"), record.get("salary_max")),
            record.get("finished_at") or record.get("started_at") or "",
            record.get("stage") or "applied",
            record.get("stage_updated_at") or "",
            _source(record),
            f"{float(record.get('score') or 0):.0f}",
            _posting_url(record),
            record.get("candidate_note") or "",
            record.get("id") or "",
        ]
        out.append([_guard(value) for value in values])
    return out


def fingerprint(values: list[str]) -> str:
    """SHA-256 of one sheet row, so an unchanged application is not sent again."""
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_key(url: str, tab: str) -> str:
    return hashlib.sha256(f"{url}\n{tab}".encode("utf-8")).hexdigest()


def _keyring():
    from .apply.email_verification import _keyring as vault
    return vault()


def _token(*, create: bool) -> str:
    vault = _keyring()
    current = vault.get_password(_SERVICE, _ACCOUNT) or ""
    if current or not create:
        return current
    with _token_lock:
        current = vault.get_password(_SERVICE, _ACCOUNT) or ""
        if current:
            return current
        generated = secrets.token_urlsafe(32)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", generated):
            raise SheetSyncError("Could not store a Google Sheets token")
        vault.set_password(_SERVICE, _ACCOUNT, generated)
        return generated


def setup_script() -> str:
    """Apps Script text with this computer's token embedded. Creates the token once."""
    token = _token(create=True)
    if not token:
        raise SheetSyncError("Could not store a Google Sheets token")
    return APPS_SCRIPT.replace(_PLACEHOLDER, token, 1)


def _destination(settings: dict) -> tuple[str, str]:
    sheet = settings.get("google_sheets") or {}
    url = str(sheet.get("web_app_url") or "").strip()
    tab = str(sheet.get("tab") or "Applications").strip()
    if not url:
        raise SheetSyncError("Add the web app URL in Settings → Google Sheets")
    if not _WEB_APP.fullmatch(url):
        raise SheetSyncError("The web app URL must be an Apps Script link ending in /exec")
    if not tab or len(tab) > 100:
        raise SheetSyncError("The tab name must be 1–100 characters")
    return url, tab


def _require_token() -> str:
    token = _token(create=False)
    if not token:
        raise SheetSyncError("Copy the setup script first so this computer has a token")
    return token


def _transport():
    """Default HTTP transport. Tests replace this; production uses httpx's own."""
    return None


def _client() -> httpx.Client:
    kwargs: dict = {"follow_redirects": True, "timeout": 60}
    transport = _transport()
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _post(url: str, body: dict) -> dict:
    """POST JSON. Apps Script answers with a redirect; httpx follows it."""
    try:
        with _client() as client:
            response = client.post(url, json=body)
    except httpx.HTTPError as error:
        detail = (str(error).strip() or type(error).__name__)[:80]
        raise SheetSyncError(f"{OFFLINE} ({detail})") from error
    try:
        data = response.json()
    except ValueError as error:
        raise SheetSyncError(f"{OFFLINE} (HTTP {response.status_code})") from error
    if not isinstance(data, dict) or not data.get("ok"):
        message = data.get("error") if isinstance(data, dict) else None
        raise SheetSyncError(str(message or "The Google Sheet refused the update"))
    return data


def test(settings: dict) -> tuple[bool, str]:
    """Ask the web app for the spreadsheet's name. Never raises for a refused sheet."""
    try:
        url, _tab = _destination(settings)
        data = _post(url, {"token": _require_token(), "action": "ping"})
    except SheetSyncError as error:
        return False, str(error)
    title = str(data.get("title") or "your spreadsheet").strip() or "your spreadsheet"
    return True, f"Connected to {title}"


def _summary(sent: int, updated: int, appended: int, total: int) -> str:
    applications = "1 application" if total == 1 else f"{total} applications"
    if sent == 0:
        return f"Google Sheet is up to date · {applications}"
    parts = []
    if appended:
        parts.append(f"added {appended}")
    if updated:
        parts.append(f"updated {updated}")
    change = " and ".join(parts) if parts else f"sent {sent}"
    return f"Google Sheet {change} · {applications}"


def sync(settings: dict, *, full: bool = False) -> SheetSyncReport:
    """Post confirmed rows that are new or changed. ``full`` sends every row."""
    url, tab = _destination(settings)
    token = _require_token()
    target = _target_key(url, tab)
    stored = str((settings.get("google_sheets") or {}).get("target") or "")
    if stored != target:
        db.clear_sheet_sync()
        db.set_settings({"google_sheets": {"target": target}})
        state: dict[int, str] = {}
    else:
        state = {} if full else db.sheet_sync_state()
    all_rows = rows()
    pending: list[tuple[int, list[str], str]] = []
    for values in all_rows:
        digest = fingerprint(values)
        app_id = int(values[_ID])
        if state.get(app_id) != digest:
            pending.append((app_id, values, digest))
    updated = appended = 0
    for offset in range(0, len(pending), BATCH):
        chunk = pending[offset:offset + BATCH]
        result = _post(url, {
            "token": token,
            "tab": tab,
            "columns": list(COLUMNS),
            "rows": [values for _app_id, values, _digest in chunk],
        })
        updated += int(result.get("updated") or 0)
        appended += int(result.get("appended") or 0)
        db.mark_sheet_synced([(app_id, digest) for app_id, _values, digest in chunk])
    sent = len(pending)
    total = len(all_rows)
    return SheetSyncReport(sent=sent, updated=updated, appended=appended, total=total,
                           summary=_summary(sent, updated, appended, total))
