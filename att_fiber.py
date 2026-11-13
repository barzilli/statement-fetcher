#!/usr/bin/env python3
"""AT&T Fiber statement connector.

Signs in to att.com using a saved browser profile, opens bill history, downloads
any bill PDFs not already filed, and files them as:

    <dest>/Utilities/AT&T Fiber/<YYYY>/<YYYY-MM-DD> AT&T Fiber bill.pdf

Usage
-----
  First time (all in the terminal; no visible browser needed, works over SSH):
      python att_fiber.py --login
  It asks for your AT&T user ID, your password and the verification code AT&T
  sends you, and types them into a headless browser.

  Every run after that (headless; reuses the saved session):
      python att_fiber.py --dest ~/Documents/Statements

  If AT&T changes its site and nothing is found (saves a screenshot and HTML):
      python att_fiber.py --debug

  Also upload to Google Drive (after a one-time  python gdrive.py --auth):
      python att_fiber.py --dest ~/Documents/Statements --drive

Exit codes: 0 ok, 2 session expired (run --login again), 3 no bills found,
4 Google Drive not authorized (run  python gdrive.py --auth).

Your password is read with a hidden prompt, passed straight to AT&T's sign-in
form and never written to disk. AT&T's own "remember this device" cookie, kept
in the saved profile, is what lets later runs work without a code. If the
sign-in page can't be recognised, --login saves a screenshot and HTML to
~/.statement-connectors/state/att_debug/ so the selectors can be updated.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from filing import StatementArchive, parse_statement_date

PROVIDER = "AT&T Fiber"
CATEGORY = "Utilities"
KIND = "bill"

HOME = Path.home() / ".statement-connectors"
DEFAULT_PROFILE = HOME / "profiles" / "att"
DEFAULT_STATE = HOME / "state"

# AT&T moves these around occasionally; adjust here if a run comes back empty.
SIGNIN_URL = "https://www.att.com/my/"
BILLING_URL = "https://www.att.com/acctmgmt/billing/mybillingcenter"

LOGIN_MARKERS = ("signin", "login", "/auth", "identity")
BILL_HISTORY_LINK = re.compile(r"bill(ing)? history|past bills|previous bills|view all bills", re.I)
# Sign-in form patterns (matched on visible labels, like the bill patterns above).
USER_FIELD = re.compile(r"user ?id|email|username", re.I)
PASSWORD_FIELD = re.compile(r"password", re.I)
CODE_FIELD = re.compile(r"(verification|security|one[- ]time|6[- ]digit)?\s*code|passcode", re.I)
SUBMIT_BUTTON = re.compile(r"^(continue|sign in|log in|next|submit|verify|confirm)$", re.I)
SEND_CODE_BUTTON = re.compile(r"send( (a|the|my))? code|text me|email me|get code", re.I)
REMEMBER_DEVICE = re.compile(r"remember|keep me signed in|trust this (device|browser)|don.t ask", re.I)
PDF_CONTROL = re.compile(r"\bpdf\b|download( bill)?|view (full )?bill|print bill", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def launch(pw, profile: Path, headed: bool) -> BrowserContext:
    profile.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=not headed,
        accept_downloads=True,
        viewport={"width": 1366, "height": 900},
        # A normal desktop UA; headless Chrome's default UA is often blocked.
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
    )


def on_login_page(page: Page) -> bool:
    return any(m in page.url.lower() for m in LOGIN_MARKERS)


# --------------------------------------------------------------------------
# Login mode
# --------------------------------------------------------------------------
def _visible(loc):
    """First visible element of a locator, or None."""
    for i in range(min(loc.count(), 10)):
        el = loc.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            pass
    return None


def _field(page: Page, label: re.Pattern, css: str):
    return (_visible(page.get_by_label(label))
            or _visible(page.get_by_placeholder(label))
            or _visible(page.locator(css)))


def _submit(page: Page, field=None) -> None:
    btn = _visible(page.get_by_role("button", name=SUBMIT_BUTTON))
    if btn:
        btn.click()
    elif field is not None:
        field.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)


def _tick_remember(page: Page) -> None:
    box = _visible(page.get_by_label(REMEMBER_DEVICE))
    if box is None:
        box = _visible(page.get_by_role("checkbox", name=REMEMBER_DEVICE))
    try:
        if box is not None and not box.is_checked():
            box.check()
    except Exception:
        pass


def _page_error(page: Page) -> str:
    alert = _visible(page.get_by_role("alert"))
    try:
        return alert.inner_text().strip() if alert else ""
    except Exception:
        return ""


def _dump(page: Page, state_dir: Path, tag: str) -> Path:
    dump = state_dir / "att_debug"
    dump.mkdir(parents=True, exist_ok=True)
    (dump / f"{tag}.html").write_text(page.content())
    page.screenshot(path=str(dump / f"{tag}.png"), full_page=True)
    return dump


def _choose_code_method(page: Page) -> bool:
    """If AT&T asks where to send the code, list the choices and let the user pick."""
    send = _visible(page.get_by_role("button", name=SEND_CODE_BUTTON))
    options = [o for o in (page.get_by_role("radio").nth(i)
                           for i in range(min(page.get_by_role("radio").count(), 8)))
               if o.is_visible()]
    if not send and not options:
        return False
    if len(options) > 1:
        labels = []
        for o in options:
            lab = o.evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : '')"
                             " || e.getAttribute('aria-label') || e.value || ''")
            labels.append(" ".join(lab.split()) or "(unlabelled option)")
        print("\nWhere should AT&T send your verification code?")
        for n, lab in enumerate(labels, 1):
            print(f"  {n}. {lab}")
        pick = input(f"Choose 1-{len(labels)} [1]: ").strip() or "1"
        idx = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(labels) else 0
        options[idx].check()
    elif options:
        options[0].check()
    if send:
        send.click()
        page.wait_for_timeout(2000)
    else:
        _submit(page)
    return True


def do_login(profile: Path, state_dir: Path, user: str | None, headed: bool) -> int:
    """Sign in from the terminal: credentials and code are typed here, the browser stays headless."""
    user = user or input("AT&T user ID or email: ").strip()
    password = getpass.getpass("AT&T password (not saved): ")
    if not user or not password:
        log("User ID and password are both needed.")
        return 2
    filled = {"user": False, "password": False}
    with sync_playwright() as pw:
        ctx = launch(pw, profile, headed=headed)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNIN_URL)
        page.wait_for_load_state("networkidle")
        ok = False
        for _ in range(12):
            if not on_login_page(page):
                page.goto(BILLING_URL)
                page.wait_for_load_state("networkidle")
                if not on_login_page(page):
                    ok = True
                    break
            err = _page_error(page)
            if err:
                log(f"AT&T says: {err}")
            code = _field(page, CODE_FIELD, "input[autocomplete='one-time-code'], input[name*='code' i]")
            pw_field = _field(page, PASSWORD_FIELD, "input[type='password']")
            user_field = _field(page, USER_FIELD, "input[name*='user' i], input[type='email']")
            if code:
                _tick_remember(page)
                value = input("Verification code from AT&T: ").strip()
                code.fill(value)
                _submit(page, code)
            elif pw_field and not (filled["password"] and err):
                if user_field and not user_field.input_value():
                    user_field.fill(user)
                    filled["user"] = True
                pw_field.fill(password)
                filled["password"] = True
                _tick_remember(page)
                _submit(page, pw_field)
            elif user_field and not filled["user"]:
                user_field.fill(user)
                filled["user"] = True
                _tick_remember(page)
                _submit(page, user_field)
            elif _choose_code_method(page):
                continue
            elif err:
                break   # e.g. wrong password; nothing more to try
            else:
                page.wait_for_timeout(3000)  # page still loading or redirecting
        if not ok:
            dump = _dump(page, state_dir, "login")
            log(f"Couldn't finish signing in. Screenshot and HTML saved to {dump}")
        ctx.close()
    password = None
    log("Signed in; session saved." if ok else "Sign-in did not complete; try --login again.")
    return 0 if ok else 2


# --------------------------------------------------------------------------
# Fetch mode
# --------------------------------------------------------------------------
def open_bill_history(page: Page) -> None:
    page.goto(BILLING_URL)
    page.wait_for_load_state("networkidle")
    if on_login_page(page):
        raise SessionExpired()
    link = page.get_by_role("link", name=BILL_HISTORY_LINK)
    if link.count() == 0:
        link = page.get_by_role("button", name=BILL_HISTORY_LINK)
    if link.count():
        link.first.click()
        page.wait_for_load_state("networkidle")
    if on_login_page(page):
        raise SessionExpired()


def find_bill_controls(page: Page):
    """Return (control, row_text) pairs for every bill PDF control on the page."""
    controls = []
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=PDF_CONTROL)
        for i in range(loc.count()):
            el = loc.nth(i)
            if not el.is_visible():
                continue
            # Walk up to the nearest container that mentions a date.
            row_text = el.evaluate(
                """el => {
                    let n = el;
                    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
                        const t = (n.innerText || '').trim();
                        if (/\\d{4}/.test(t) && t.length < 400) return t;
                    }
                    return (el.getAttribute('aria-label') || el.innerText || '');
                }"""
            )
            controls.append((el, row_text))
    return controls


def download_one(ctx: BrowserContext, page: Page, control, out_dir: Path) -> Path:
    """Click a bill control and capture the PDF whether it downloads or opens a tab."""
    out = out_dir / f"att_{hashlib.md5(str(id(control)).encode()).hexdigest()[:8]}.pdf"
    try:
        with page.expect_download(timeout=20_000) as dl:
            control.click()
        dl.value.save_as(out)
        return out
    except PWTimeout:
        pass
    # Some bills open in a new tab as an inline PDF instead of downloading.
    for p in ctx.pages:
        if p is not page and (".pdf" in p.url.lower() or "bill" in p.url.lower()):
            resp = ctx.request.get(p.url)
            out.write_bytes(resp.body())
            p.close()
            return out
    raise RuntimeError("clicked the bill control but no PDF arrived")


def make_drive(root_name: str):
    from gdrive import DriveNotAuthorized, DriveUploader  # only needed with --drive
    try:
        return DriveUploader(root_name)
    except DriveNotAuthorized as e:
        log(str(e))
        return None


def fetch(dest: Path, profile: Path, state_dir: Path, headed: bool,
          max_bills: int, debug: bool, drive_root: str | None = None) -> int:
    drive = make_drive(drive_root) if drive_root else None
    if drive_root and not drive:
        return 4
    archive = StatementArchive(dest, CATEGORY, PROVIDER, state_dir, drive=drive)
    filed, skipped, failed = [], 0, 0

    with sync_playwright() as pw, tempfile.TemporaryDirectory() as tmp:
        ctx = launch(pw, profile, headed)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            open_bill_history(page)
        except SessionExpired:
            ctx.close()
            archive.upload_pending(log)  # still push anything left from earlier runs
            log("AT&T session expired. Run:  python att_fiber.py --login")
            return 2

        controls = find_bill_controls(page)
        if debug:
            dump = state_dir / "att_debug"
            dump.mkdir(parents=True, exist_ok=True)
            (dump / "page.html").write_text(page.content())
            page.screenshot(path=str(dump / "page.png"), full_page=True)
            log(f"Debug snapshot saved to {dump}")
        if not controls:
            ctx.close()
            log("No bill PDF links found. AT&T may have changed the page; run with --debug.")
            return 3

        for control, row_text in controls[:max_bills]:
            stmt_date = parse_statement_date(row_text)
            if not stmt_date:
                log(f"Skipping a row with no recognisable date: {row_text[:60]!r}")
                failed += 1
                continue
            key = stmt_date.isoformat()
            if archive.has(key):
                skipped += 1
                continue
            try:
                pdf = download_one(ctx, page, control, Path(tmp))
                path = archive.file_statement(pdf, stmt_date, key, KIND)
                filed.append(path)
                log(f"Filed {path}")
            except Exception as e:  # keep going; one bad bill shouldn't stop the rest
                failed += 1
                log(f"Failed {stmt_date}: {e}")
        ctx.close()

    log(f"Done: {len(filed)} new, {skipped} already filed, {failed} problems.")
    if drive:
        up, bad = archive.upload_pending(log)
        log(f"Drive: {up} uploaded, {bad} pending retry.")
    return 0


class SessionExpired(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true",
                    help="sign in from the terminal (headless) and save the session")
    ap.add_argument("--user", help="AT&T user ID for --login (prompted if omitted)")
    ap.add_argument("--dest", type=Path, default=Path.home() / "Documents" / "Statements",
                    help="archive root (default: ~/Documents/Statements)")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="saved browser profile folder")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE, help="where 'already filed' records live")
    ap.add_argument("--headed", action="store_true", help="show the browser window (optional, for troubleshooting only)")
    ap.add_argument("--max-bills", type=int, default=24, help="most bills to consider per run")
    ap.add_argument("--debug", action="store_true", help="save a screenshot and HTML of the bill page")
    ap.add_argument("--drive", nargs="?", const="Statements", metavar="FOLDER",
                    help="also upload to this top-level Google Drive folder (default: Statements)")
    a = ap.parse_args()

    if a.login:
        return do_login(a.profile, a.state, a.user, a.headed)
    return fetch(a.dest, a.profile, a.state, a.headed, a.max_bills, a.debug, a.drive)


if __name__ == "__main__":
    sys.exit(main())
