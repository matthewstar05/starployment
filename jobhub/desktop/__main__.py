"""Launch Starployment as a native desktop application: python -m jobhub.desktop."""
from __future__ import annotations

import argparse
import os
import signal
import sys
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
            browser = playwright.chromium.launch(headless=True)
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
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Starployment")
    app.setApplicationDisplayName("Starployment")
    app.setFont(app_font())
    apply_application_icon(app)
    apply_appearance(app, "System")
    window = JobHubWindow()
    controller = DesktopController(window, scheduler_enabled=not (args.no_scheduler or args.smoke_test or args.setup),
                                   show_setup_on_start=not args.smoke_test,
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
            window.grab().save(str(path), "PNG")
            app.quit()
        QTimer.singleShot(500, finish)
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())


def setup_main() -> int:
    """Dedicated `jobhub-setup` console entrypoint for candidate configuration."""
    return main(["--setup", *sys.argv[1:]])
