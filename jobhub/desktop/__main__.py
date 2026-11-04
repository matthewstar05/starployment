"""Launch Starployment as a native desktop application: python -m jobhub.desktop."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def quit_on_termination(app: QApplication) -> None:
    """Close cleanly on SIGTERM or SIGINT (the installer, logout, Ctrl-C).

    Qt's event loop runs in C++, so Python only runs signal handlers when it
    regains control. A light timer hands it control while the app is idle.
    """
    def request_quit(*_args) -> None:
        QTimer.singleShot(0, app.quit)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_quit)
    wake = QTimer(app)
    wake.timeout.connect(lambda: None)
    wake.start(500)


NETWORK_TIMEOUT_SECONDS = 120
SEND_GRACE_SECONDS = 900     # a send's own timeouts end it well before this


def sending_now() -> bool:
    """Whether an application is queued or being sent, read on a connection of its own
    (the shared one may be held by a worker thread that is still running)."""
    import sqlite3
    from jobhub import config    # absolute: in the app bundle this file runs as the top-level script
    try:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            return con.execute("SELECT 1 FROM applications WHERE status IN ('queued','running') LIMIT 1"
                               ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True     # unsure: let the send finish


def hard_exit(code: int) -> None:
    """End the process now, skipping Python's wait for worker threads."""
    if sys.platform == "win32":
        # os._exit still runs every DLL's process-detach code, and Qt's crashes
        # with an access violation while the application object is alive.
        # TerminateProcess ends the process at once, as os._exit is meant to.
        import ctypes

        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.TerminateProcess(kernel.GetCurrentProcess(), code & 0xFFFFFFFF)
    os._exit(code)


def exit_promptly(code: int) -> int:
    """End the process once the window has quit, unless an application is being sent.

    Python waits for every background thread at exit, so a scan, an inbox check, or
    agent calls kept a quitting app alive for minutes (and made the installer give
    up). Interrupted scans roll back, and unfinished drafts are recovered at the next
    start (db.recover_drafts). A send in progress still finishes first; returning
    to the normal exit instead left the process waiting on idle workers for good.
    """
    deadline = time.monotonic() + SEND_GRACE_SECONDS
    try:
        while sending_now() and time.monotonic() < deadline:
            time.sleep(1)
    except Exception:  # noqa: BLE001 - whatever happens, never fall back to waiting on idle threads
        pass
    # PyInstaller's Windows windowed launcher has no standard streams. A
    # closed console must not prevent shutdown after sends have finished.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
    hard_exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="starployment")
    parser.add_argument("--no-scheduler", action="store_true", help="open the desktop app without background scans")
    parser.add_argument("--setup", action="store_true", help="open candidate setup directly")
    parser.add_argument("--smoke-test", metavar="PNG", help="render a screenshot and exit without network activity")
    parser.add_argument("--browser-check", action="store_true", help="verify the bundled Chromium can launch")
    parser.add_argument("--dependency-check", action="store_true", help="verify native discovery dependencies can load")
    parser.add_argument("--native-glass-check", metavar="DIRECTORY",
                        help="run a standalone Cocoa Liquid Glass compositor diagnostic")
    args = parser.parse_args(argv)
    if args.smoke_test:
        # Windowed bundles have no stderr, so a native crash would vanish
        # without a trace. Keep the Python stack beside the screenshot.
        import faulthandler

        fault_log = Path(args.smoke_test).with_suffix(".fault.txt")
        fault_log.parent.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(open(fault_log, "w", encoding="utf-8"), all_threads=True)  # noqa: SIM115 - lives for the process
    # A network read with no timeout of its own (a scraping library, a mail check) gives up after
    # this long instead of waiting forever when the connection drops mid-reply (sleep, Wi-Fi).
    import socket
    socket.setdefaulttimeout(NETWORK_TIMEOUT_SECONDS)
    if args.native_glass_check:
        # This must stay before source-data migration and all application/controller imports.
        from jobhub.desktop.native_glass_check import run

        return run(args.native_glass_check)
    if args.dependency_check:
        import jobspy  # noqa: F401  (loads tls_client and its platform-specific native library)

        return 0
    if args.browser_check:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            # Both the reduced headless shell and the full browser in headless mode,
            # which is how applications are sent.
            for channel in (None, "chromium"):
                browser = playwright.chromium.launch(headless=True, channel=channel)
                try:
                    page = browser.new_page()
                    page.goto("about:blank")
                finally:
                    browser.close()
        return 0
    if args.smoke_test:
        # A still frame: skip entrance animations so the screenshot is final.
        os.environ.setdefault("JOBHUB_REDUCED_MOTION", "1")
    from jobhub.desktop.controller import DesktopController
    from jobhub.desktop.icon import apply_application_icon
    from jobhub.desktop.styles import app_font, apply_appearance
    from jobhub.desktop.ui import JobHubWindow

    migration_error = ""
    if getattr(sys, "frozen", False) and not args.smoke_test and not os.environ.get("JOBHUB_DATA_DIR"):
        from jobhub.migration import bundled_checkout_data, migrate_source_checkout

        checkout_data = bundled_checkout_data()
        if checkout_data is not None:
            try:
                migrate_source_checkout(checkout_data)
            except Exception as error:  # noqa: BLE001
                migration_error = str(error)
    from jobhub.desktop.native_windows import set_app_user_model_id

    set_app_user_model_id()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Starployment")
    app.setApplicationDisplayName("Starployment")
    app.setFont(app_font())
    apply_application_icon(app)
    apply_appearance(app, "System")
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=not (args.no_scheduler or args.smoke_test or args.setup),
                                   show_setup_on_start=not args.smoke_test or args.setup,
                                   force_setup_on_start=args.setup)
    if migration_error:
        from PySide6.QtWidgets import QMessageBox

        QTimer.singleShot(0, lambda: QMessageBox.warning(
            window, "Could not import existing setup",
            "Starployment could not copy the setup from this checkout. The existing data was kept.\n\n" + migration_error,
        ))
    app.aboutToQuit.connect(controller.stop)
    quit_on_termination(app)
    window.show()
    if args.smoke_test:
        def finish():
            path = Path(args.smoke_test)
            path.parent.mkdir(parents=True, exist_ok=True)
            target = app.activeModalWidget() if args.setup else window
            if target is None or (args.setup and target.__class__.__name__ != "SetupWizard"):
                # Leave no screenshot on failure so smoke_bundle rejects it.
                app.exit(1)
                return
            target.grab().save(str(path), "PNG")
            if args.setup:
                # Render the actual first-run connection controls as well.
                target.tabs.setCurrentIndex(target.tabs.count() - 1)
                app.processEvents()
                target.grab().save(str(path.with_name(path.stem + "-connections.png")), "PNG")
                target.reject()
            app.quit()
        QTimer.singleShot(500, finish)
        # Exercise the same shutdown path as the installed windowed app,
        # including platforms where stdout/stderr do not exist.
        return exit_promptly(int(app.exec()))
    return exit_promptly(int(app.exec()))


if __name__ == "__main__":
    raise SystemExit(main())


def setup_main() -> int:
    """Dedicated `jobhub-setup` console entrypoint for candidate configuration."""
    return main(["--setup", *sys.argv[1:]])
