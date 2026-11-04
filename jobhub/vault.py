"""The candidate's Workday sign-ins, kept on this computer only.

Starployment creates each employer's Workday account with a generated
password (``settings.workday.create_accounts``) or saves the one the
candidate types when signing in themselves, so later applications sign in on
their own. Passwords go through the same vault as the mailbox password
(``email_verification._keyring``: macOS Keychain with Settings → Security →
Keychain protection on, else the owner-only ``secrets.json``), never SQLite or
logs. ``accounts.json`` beside it holds only what is safe to show: employer,
email, dates, status. Showing or copying a password in the app asks for Touch
ID or the Mac's login password first (``authenticate``).
"""
from __future__ import annotations

import json
import os
import secrets
import string
import sys
import threading
from datetime import datetime, timezone

from . import config

SERVICE = "Starployment Workday accounts"
# Workday tenants require upper, lower, digit, and special characters; some reject quotes and spaces.
SPECIALS = "!@#$%^&*-_=+?"
STATUSES = ("active", "creating", "needs_verification", "exists", "failed")
STATUS_LABELS = {"active": "Signed in", "creating": "Being created", "needs_verification": "Waiting for email verification",
                 "exists": "Account already existed: sign in once yourself", "failed": "Sign-in failed"}
_lock = threading.Lock()
_PENDING: list = []   # Touch ID replies that may still arrive after a timeout


def _path():
    return config.DATA / "accounts.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tenant_of(url_or_host: str) -> str:
    """The Workday tenant an account belongs to: ``citi`` for citi.wd5.myworkdayjobs.com."""
    from urllib.parse import urlparse
    host = (urlparse(url_or_host).hostname if "//" in url_or_host else url_or_host) or ""
    host = host.casefold()
    if not host.endswith(".myworkdayjobs.com") or host.count(".") != 3:
        raise ValueError(f"Not a Workday address: {url_or_host[:80]}")
    return host.split(".")[0]


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        config.secure_file(temporary)
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)


def _vault():
    from .apply.email_verification import _keyring
    return _keyring()


def _key(tenant: str, email: str) -> str:
    return f"{tenant}:{email.strip().casefold()}"


def generate_password(length: int = 20) -> str:
    """A random password meeting every Workday tenant's rules (upper, lower, digit, special)."""
    alphabet = string.ascii_letters + string.digits + SPECIALS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in SPECIALS for c in password)
                and password[0].isalnum()):
            return password


def list_accounts() -> list[dict]:
    """Saved accounts without their passwords, newest first."""
    return sorted(_load().values(), key=lambda a: a.get("updated_at", ""), reverse=True)


def get_account(tenant: str) -> dict | None:
    return _load().get(tenant.casefold())


def save_account(tenant: str, email: str, password: str, *, company: str = "", host: str = "",
                 status: str = "active", note: str = "") -> dict:
    """Store a password in the vault and its public details in accounts.json."""
    if not tenant or not email.strip() or not password:
        raise ValueError("An employer, email, and password are required")
    if status not in STATUSES:
        raise ValueError(f"Unknown account status: {status}")
    tenant = tenant.casefold()
    with _lock:
        data = _load()
        previous = data.get(tenant) or {}
        if previous.get("email") and previous["email"] != email.strip().casefold():
            _forget_password(tenant, previous["email"])
        _vault().set_password(SERVICE, _key(tenant, email), password)
        record = {"tenant": tenant, "company": company or previous.get("company") or tenant,
                  "host": host or previous.get("host", ""), "email": email.strip().casefold(),
                  "created_at": previous.get("created_at") or _now(), "updated_at": _now(),
                  "last_used": previous.get("last_used", ""), "status": status, "note": note[:200]}
        data[tenant] = record
        _save(data)
    return dict(record)


def set_status(tenant: str, status: str, note: str = "", used: bool = False) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown account status: {status}")
    with _lock:
        data = _load()
        record = data.get(tenant.casefold())
        if record is None:
            return
        record.update(status=status, note=note[:200], updated_at=_now())
        if used:
            record["last_used"] = _now()
        _save(data)


def password_for(tenant: str) -> str | None:
    """The saved password, for signing in. The UI shows it only after ``authenticate``."""
    record = get_account(tenant)
    if not record:
        return None
    try:
        return _vault().get_password(SERVICE, _key(record["tenant"], record["email"]))
    except Exception:  # noqa: BLE001 - a locked vault means no saved sign-in this time
        return None


def _forget_password(tenant: str, email: str) -> None:
    vault = _vault()
    try:
        vault.delete_password(SERVICE, _key(tenant, email))
    except Exception:  # noqa: BLE001 - already gone
        pass


def delete_account(tenant: str) -> None:
    """Forget a saved sign-in here. The account on the employer's Workday stays."""
    with _lock:
        data = _load()
        record = data.pop(tenant.casefold(), None)
        if record:
            _forget_password(record["tenant"], record["email"])
        _save(data)


# ------------------------------------------------------------------ Touch ID
def authentication_available() -> bool:
    return sys.platform == "darwin"


def authenticate(reason: str, timeout: float = 120) -> tuple[bool, str]:
    """Ask for Touch ID (or the Mac's login password). Returns (ok, message).

    Uses LocalAuthentication's ``LAContext`` with the device-owner policy, so a
    Mac without Touch ID falls back to its password. Off macOS there is no
    system prompt to ask, so passwords stay hidden.
    """
    if not authentication_available():
        return False, "Showing saved passwords needs macOS."
    try:
        return _la_evaluate(2, reason, timeout)   # LAPolicyDeviceOwnerAuthentication
    except Exception as exc:  # noqa: BLE001 - never show a password when the check itself failed
        return False, f"The system could not ask for Touch ID: {str(exc)[:120]}"


def _la_evaluate(policy: int, reason: str, timeout: float, interactive: bool = True) -> tuple[bool, str]:
    """LAContext evaluatePolicy:localizedReason:reply: through the Objective-C runtime.

    The reply is an Objective-C block; a global block literal (flags
    BLOCK_IS_GLOBAL) needs no copy helpers and is laid out by hand here.
    """
    import ctypes
    import ctypes.util

    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary("/System/Library/Frameworks/LocalAuthentication.framework/LocalAuthentication")
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
    system = ctypes.CDLL(ctypes.util.find_library("System"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(restype, argtypes):
        function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        return function(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)

    def sel(name: str):
        return objc.sel_registerName(name.encode())

    done = threading.Event()
    outcome: dict = {}

    reply_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p)

    def reply(_block, success, error):
        message = ""
        if error:
            description = send(ctypes.c_void_p, [])(error, sel("localizedDescription"))
            if description:
                raw = send(ctypes.c_char_p, [])(description, sel("UTF8String"))
                message = (raw or b"").decode("utf-8", "replace")
        outcome.update(ok=bool(success), message=message)
        done.set()

    callback = reply_type(reply)

    class Descriptor(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]

    class Block(ctypes.Structure):
        _fields_ = [("isa", ctypes.c_void_p), ("flags", ctypes.c_int), ("reserved", ctypes.c_int),
                    ("invoke", ctypes.c_void_p), ("descriptor", ctypes.POINTER(Descriptor))]

    descriptor = Descriptor(0, ctypes.sizeof(Block))
    global_block_isa = ctypes.c_void_p.in_dll(system, "_NSConcreteGlobalBlock")
    block = Block(ctypes.addressof(global_block_isa), 1 << 28, 0,
                  ctypes.cast(callback, ctypes.c_void_p).value, ctypes.pointer(descriptor))

    context_class = objc.objc_getClass(b"LAContext")
    string_class = objc.objc_getClass(b"NSString")
    if not context_class:
        return False, "LocalAuthentication is unavailable."
    context = send(ctypes.c_void_p, [])(send(ctypes.c_void_p, [])(context_class, sel("alloc")), sel("init"))
    text = send(ctypes.c_void_p, [ctypes.c_char_p])(string_class, sel("stringWithUTF8String:"), reason.encode())
    if not interactive:   # tests: fail at once instead of showing the prompt
        send(None, [ctypes.c_bool])(context, sel("setInteractionNotAllowed:"), True)
    try:
        send(None, [ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p])(
            context, sel("evaluatePolicy:localizedReason:reply:"), policy, text, ctypes.addressof(block))
        if not done.wait(timeout):
            # The prompt may still answer later: keep the block alive for it.
            _PENDING.append((callback, block, descriptor))
            send(None, [])(context, sel("invalidate"))
            return False, "Touch ID timed out."
    finally:
        send(None, [])(context, sel("release"))
    return bool(outcome.get("ok")), outcome.get("message", "")


# ------------------------------------------------------------------ your own sign-ins
def parse_site(address: str) -> tuple[str, str]:
    """(tenant, host) from an employer's Workday address or host, e.g.
    ``https://citi.wd5.myworkdayjobs.com/2`` → ("citi", "citi.wd5.myworkdayjobs.com")."""
    from urllib.parse import urlparse
    text = (address or "").strip()
    host = (urlparse(text if "//" in text else f"https://{text}").hostname or "").casefold()
    return tenant_of(host), host


def add_own(address: str, email: str, password: str, company: str = "") -> dict:
    """A sign-in the candidate already has (or just changed), entered by hand."""
    tenant, host = parse_site(address)
    if "@" not in (email or ""):
        raise ValueError("Enter the email you sign in with")
    return save_account(tenant, email, password, company=company.strip() or tenant.title(), host=host,
                        status="active", note="Added by you")


def site_url(record: dict) -> str:
    """The employer's Workday address for a saved account ('' when unknown)."""
    host = record.get("host") or ""
    return f"https://{host}/" if host else ""


# ------------------------------------------------------------------ macOS Passwords
# The Passwords app imports this CSV layout (File → Import Passwords…). Starployment is signed
# on this Mac without an Apple-issued keychain entitlement, so it cannot write into the
# Passwords keychain itself; an import is the supported way in, and Passwords then syncs the
# sign-ins with iCloud Keychain and fills them in Safari on each employer's Workday.
PASSWORDS_HEADER = ("Title", "URL", "Username", "Password", "Notes", "OTPAuth")
EXPORT_NAME = "Starployment Workday sign-ins.csv"
EXPORT_SECONDS = 10 * 60


def export_dir():
    return config.DATA / "passwords-export"


def export_for_passwords() -> tuple[object, int]:
    """Write every saved sign-in to an owner-only CSV for the Passwords app. Returns (path, count).

    Callers ask for Touch ID first and remove the file with ``clear_export`` soon after.
    """
    import csv
    import io
    rows = []
    for record in list_accounts():
        password = password_for(record["tenant"])
        if not password:
            continue
        rows.append((f"{record.get('company') or record['tenant']} (Workday)", site_url(record),
                     record["email"], password, "Saved by Starployment", ""))
    if not rows:
        raise ValueError("There are no saved sign-ins with a password to export yet.")
    folder = export_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    path = folder / EXPORT_NAME
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(PASSWORDS_HEADER)
    writer.writerows(rows)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())
    return path, len(rows)


def clear_export() -> None:
    """Delete an export file (after the import, on a timer, and at startup)."""
    folder = export_dir()
    try:
        for item in folder.iterdir():
            item.unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass


def sign_in_blocker(apply_url: str) -> str:
    """Why an unattended Workday send to this posting would stop at sign-in ('' when it would not).

    An account Starployment could not sign in to (one that already existed, or a refused
    password) needs the candidate once, in Workday sign-ins; trying anyway would only record
    an attempt that cannot go anywhere.
    """
    import re
    shared = re.match(r"https?://wd\d+\.myworkdaysite\.com/recruiting/([A-Za-z0-9_-]+)/", apply_url or "", re.I)
    try:
        tenant = shared.group(1).casefold() if shared else tenant_of(apply_url)
    except ValueError:
        return ""
    record = get_account(tenant)
    if not record or record.get("status") not in ("exists", "failed"):
        return ""
    company = record.get("company") or tenant
    return (f"Starployment cannot sign in to {company}'s Workday yet. Open File → Workday Sign-ins, choose Edit "
            f"for {company}, and enter your password; JARVIS applies there again after that.")
