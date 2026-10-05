"""Semantic, appearance-aware stylesheet for the native Starployment desktop client.

Stylesheet rules cover stock Qt controls (labels, inputs, buttons, tables,
scroll bars, menus). Glass surfaces, the sidebar pill, switches, segmented
controls, and toasts are custom-painted in ``widgets.py`` and deliberately
have no stylesheet rules. Label roles are selected by ``objectName``; see the
"Typography" block below for the available roles.
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from . import fonts
from .icons import icon_path
from .theme import DARK, colors_for, resolve_appearance


def app_font(size: int = 13, weight: int = QFont.Weight.Normal) -> QFont:
    """The interface font at ``size`` pixels: Anthropic Sans when Claude's type is in use,
    otherwise the platform UI font (SF Pro via ``.AppleSystemUIFont`` on macOS)."""
    family = fonts.ui_family()
    if family:
        font = QFont(family)
    else:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    return font


def stylesheet(appearance: str | None = None) -> str:
    """Build the global stylesheet for System, Light, or Dark appearance."""
    scheme = resolve_appearance(appearance)
    c = colors_for(scheme)
    dark = scheme == DARK
    # Claude's Anthropic Sans when available (fonts.py); otherwise the native system family,
    # since Qt does not register the SF Pro marketing name on macOS.
    family = fonts.ui_family()
    font_rule = f"font-family: '{family}';" if family else ""
    display = fonts.display_family()
    # Page titles use Claude's serif at a regular weight, the way Claude sets its headings.
    title_rule = f"font-family: '{display}'; font-weight: 500;" if fonts.active() == fonts.CLAUDE \
        else "font-weight: 700;"
    brand_rule = f"font-family: '{display}'; font-size: 19px; font-weight: 600;" if fonts.active() == fonts.CLAUDE \
        else "font-size: 17px; font-weight: 700;"
    check_icon = icon_path("check").as_posix()
    up_icon = icon_path("chevron-up").as_posix()
    down_icon = icon_path("chevron-down").as_posix()
    control_line = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.09)"
    control_line_hover = "rgba(255,255,255,0.16)" if dark else "rgba(0,0,0,0.16)"
    secondary_fill = "rgba(255,255,255,0.09)" if dark else "rgba(255,255,255,0.82)"
    secondary_hover = "rgba(255,255,255,0.14)" if dark else "rgba(255,255,255,1.0)"
    secondary_pressed = "rgba(255,255,255,0.06)" if dark else "rgba(235,236,240,1.0)"
    primary_top = c['button_top']
    scroll_handle = "rgba(255,255,255,0.22)" if dark else "rgba(0,0,0,0.20)"
    scroll_handle_hover = "rgba(255,255,255,0.34)" if dark else "rgba(0,0,0,0.32)"
    # Notes and logs inside a panel read as soft wells, not darker boxes with borders.
    inset_fill = c['fill_inset']
    # The activity console is a dark terminal in Dark mode and a soft white panel in Light mode.
    console_fill = "#0E0E12" if dark else "rgba(255,255,255,0.72)"
    console_line = "rgba(255,255,255,0.07)" if dark else "rgba(0,0,0,0.07)"
    # Amber notices are a light wash over the panel, not a solid block.
    notice_fill = "rgba(255,196,64,0.11)" if dark else "rgba(255,170,0,0.13)"
    return f"""
    /* Base ------------------------------------------------------------- */
    QWidget {{ color: {c['text']}; {font_rule} font-size: 13px; }}
    QMainWindow, QDialog {{ background-color: {c['window']}; color: {c['text']}; }}
    QMainWindow[jobhubNativeGlassPrepared="true"], QDialog[jobhubNativeGlassPrepared="true"] {{
        background: transparent; }}
    QLabel {{ background-color: transparent; }}
    QWidget#page, QWidget#formPage, QWidget#formViewport, QScrollArea#formScroll,
    QScrollArea, QScrollArea > QWidget > QWidget, QStackedWidget, QSplitter,
    QWidget#formRow, QWidget#toolbar {{ background: transparent; border: none; }}
    QSplitter::handle {{ background: transparent; }}

    /* Typography (select with objectName) ------------------------------ */
    QLabel#brand {{ color: {c['text']}; {brand_rule} }}
    QLabel#eyebrow {{ color: {c['text_muted']}; font-size: 11px; font-weight: 600; }}
    QLabel#pageTitle {{ color: {c['text']}; font-size: 28px; {title_rule} }}
    QLabel#sheetTitle {{ color: {c['text']}; font-size: 22px; {title_rule} }}
    QLabel#subtitle {{ color: {c['text_muted']}; font-size: 13px; }}
    QLabel#caption {{ color: {c['text_subtle']}; font-size: 11px; }}
    QLabel#sectionTitle, QLabel#cardTitle {{ color: {c['text']}; font-size: 15px; font-weight: 600; }}
    QLabel#groupTitle {{ color: {c['text']}; font-size: 13px; font-weight: 600; }}
    QLabel#groupNote {{ color: {c['text_muted']}; font-size: 11px; }}
    QLabel#rowTitle {{ color: {c['text']}; font-size: 13px; }}
    QLabel#rowNote {{ color: {c['text_muted']}; font-size: 11px; }}
    QLabel#emptyTitle {{ color: {c['text']}; font-size: 15px; font-weight: 600; }}
    QLabel#metricValue {{ color: {c['text']}; font-size: 30px; font-weight: 700; }}
    QLabel#metricLabel {{ color: {c['text_muted']}; font-size: 12px; font-weight: 500; }}
    QLabel#activityTime {{ color: {c['text_subtle']}; font-size: 11px; }}
    QLabel#activityMessage {{ color: {c['text']}; font-size: 13px; }}
    QLabel#rowPrimary {{ color: {c['text']}; font-size: 13px; font-weight: 600; }}
    QLabel#rowSecondary {{ color: {c['text_muted']}; font-size: 12px; }}
    QLabel#sidebarStatus {{ color: {c['text_muted']}; font-size: 11px; }}
    QLabel#sidebarStatusTitle {{ color: {c['text']}; font-size: 12px; font-weight: 600; }}
    QLabel#detailTitle {{ color: {c['text']}; font-size: 20px; font-weight: 700; }}
    QLabel#pill {{ border-radius: 10px; padding: 3px 10px; font-size: 11px; font-weight: 600; }}
    QLabel#callout {{ background: {inset_fill}; color: {c['text']}; border: none;
        border-radius: 12px; padding: 11px 13px; }}
    QLabel#questions {{ background: {notice_fill}; color: {c['text']}; border-radius: 12px; padding: 11px 13px; }}
    QLabel#dropHint {{ color: {c['text_muted']}; font-size: 12px; }}
    QFrame#console {{ background: {console_fill}; border: 1px solid {console_line}; border-radius: 12px; }}
    QFrame#console QLabel#consoleTitle {{ color: {c['text']}; font-size: 12px; font-weight: 600; }}
    QFrame#console QLabel#consoleDetail {{ color: {c['text_muted']}; font-size: 11px; }}
    QPlainTextEdit#consoleLog {{ background: transparent; border: none; padding: 0; color: {c['text']};
        selection-background-color: {c['accent_soft']}; selection-color: {c['text']}; }}
    QPlainTextEdit#consoleLog:focus {{ border: none; padding: 0; }}

    /* Inputs ------------------------------------------------------------ */
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background-color: {c['fill_control']}; color: {c['text']};
        border: 1px solid {control_line}; border-radius: 9px; padding: 6px 10px;
        selection-background-color: {c['button_fill']}; selection-color: #FFFFFF; }}
    QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{
        border-color: {control_line_hover}; }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 2px solid {c['accent']}; padding: 5px 9px; background-color: {c['fill_control_hover']}; }}
    QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        color: {c['disabled']}; background-color: {c['fill_quiet']}; }}
    QLineEdit#search {{ border-radius: 15px; padding: 5px 10px 5px 4px; min-height: 18px; }}
    QLineEdit#search:focus {{ padding: 4px 9px 4px 3px; }}
    QLineEdit#search[nativeGlass="true"], QLineEdit#search[nativeGlass="true"]:hover,
    QLineEdit#search[nativeGlass="true"]:focus {{ background: transparent; }}
    QPlainTextEdit#evidence {{ background: {inset_fill}; border: none; border-radius: 12px;
        padding: 10px; color: {c['text_muted']}; font-size: 12px; }}
    QSpinBox {{ padding-right: 22px; min-width: 64px; }}
    QSpinBox[stepper="true"] {{ padding-right: 10px; min-width: 0; }}
    QSpinBox[stepper="true"]:focus {{ padding-right: 9px; }}
    QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 18px; border: none;
        background: transparent; margin-right: 3px; }}
    QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 3px; }}
    QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 3px; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c['fill_hover']}; border-radius: 4px; }}
    QSpinBox::up-arrow {{ image: url("{up_icon}"); width: 9px; height: 9px; }}
    QSpinBox::down-arrow {{ image: url("{down_icon}"); width: 9px; height: 9px; }}
    QLineEdit#tagInput {{ background: transparent; border: 1px dashed {control_line_hover}; border-radius: 13px;
        padding: 2px 10px; font-size: 12px; }}
    QLineEdit#tagInput:focus {{ border: 1px solid {c['accent']}; padding: 2px 10px; background: {c['fill_control_hover']}; }}
    QToolButton#rowRemove {{ background: transparent; border: none; border-radius: 10px; padding: 4px; }}
    QToolButton#rowRemove:hover {{ background: {c['fill_quiet']}; }}
    QLabel#heroTitle {{ color: #FFFFFF; font-size: 24px; {title_rule} }}
    QLabel#heroText {{ color: rgba(255,255,255,0.74); font-size: 13px; }}
    QLabel#heroStat {{ color: rgba(255,255,255,0.86); font-size: 12px; font-weight: 600; }}
    QLabel#sourceBadge {{ color: {c['text_muted']}; font-size: 11px; font-weight: 600; }}
    QLabel#questionLabel {{ color: {c['text']}; font-size: 13px; font-weight: 600; }}
    QLabel#lunaNote {{ color: {c['indigo']}; font-size: 11px; }}
    QLabel#statValue {{ color: {c['text']}; font-size: 22px; font-weight: 700; }}
    QLabel#statLabel {{ color: {c['text_muted']}; font-size: 11px; font-weight: 600; }}
    QComboBox {{ padding-right: 26px; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox QAbstractItemView {{ background: {c['surface']}; border: 1px solid {control_line}; border-radius: 8px;
        selection-background-color: {c['button_fill']}; selection-color: #FFFFFF; padding: 4px; }}

    /* Buttons: capsules ------------------------------------------------- */
    QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {primary_top}, stop:1 {c['button_fill']});
        color: #FFFFFF; border: 1px solid rgba(0,0,0,0.06); border-radius: 15px;
        padding: 6px 16px; min-height: 18px; font-weight: 600; }}
    QPushButton:hover {{ background: {c['button_hover']}; }}
    QPushButton:pressed {{ background: {c['button_pressed']}; }}
    QPushButton:focus {{ border: 2px solid rgba(255,255,255,0.55); padding: 5px 15px; }}
    QPushButton:disabled {{ background: {c['fill_quiet']}; color: {c['disabled']}; border-color: transparent; }}
    QPushButton#secondary {{ background: {secondary_fill}; color: {c['text']}; border: 1px solid {control_line}; }}
    QPushButton#secondary:hover {{ background: {secondary_hover}; border-color: {control_line_hover}; }}
    QPushButton#secondary:pressed {{ background: {secondary_pressed}; }}
    QPushButton#secondary:focus, QPushButton#quiet:focus, QPushButton#danger:focus {{
        border: 2px solid {c['accent']}; padding: 5px 15px; }}
    QPushButton#secondary:disabled {{ background: {c['fill_quiet']}; color: {c['disabled']}; border-color: transparent; }}
    QPushButton#quiet {{ background: transparent; color: {c['accent_text']}; border: 1px solid transparent; padding: 6px 10px; }}
    QPushButton#quiet:hover {{ background: {c['fill_hover']}; }}
    QPushButton#quiet:pressed {{ background: {c['fill_quiet']}; }}
    QPushButton#danger {{ background: {c['red_soft']}; color: {c['red']}; border: 1px solid transparent; }}
    QPushButton#star {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFD866, stop:1 {c['star']});
        color: #14110A; border: 1px solid rgba(0,0,0,0.10); border-radius: 22px; padding: 10px 30px;
        min-height: 22px; font-size: 16px; font-weight: 700; }}
    QPushButton#star:hover {{ background: #FFE07A; }}
    QPushButton#star:pressed {{ background: {c['star']}; }}
    QPushButton#star:focus {{ border: 2px solid #FFFFFF; padding: 9px 29px; }}
    QPushButton#star:disabled {{ background: rgba(255,255,255,0.18); color: rgba(255,255,255,0.5); }}
    QPushButton#danger:hover {{ background: {c['red_soft']}; border-color: {c['red']}; }}
    QPushButton[nativeGlass="true"], QPushButton[nativeGlass="true"]:hover,
    QPushButton[nativeGlass="true"]:pressed, QPushButton[nativeGlass="true"]:disabled,
    QPushButton#secondary[nativeGlass="true"], QPushButton#secondary[nativeGlass="true"]:hover,
    QPushButton#secondary[nativeGlass="true"]:pressed, QPushButton#secondary[nativeGlass="true"]:disabled,
    QPushButton#quiet[nativeGlass="true"], QPushButton#quiet[nativeGlass="true"]:hover,
    QPushButton#quiet[nativeGlass="true"]:pressed,
    QPushButton#danger[nativeGlass="true"], QPushButton#danger[nativeGlass="true"]:hover {{
        background: transparent; border-color: transparent; }}
    QPushButton[nativeGlass="true"]:focus, QPushButton#secondary[nativeGlass="true"]:focus,
    QPushButton#quiet[nativeGlass="true"]:focus, QPushButton#danger[nativeGlass="true"]:focus {{
        border-color: {c['accent']}; }}

    /* Tables and lists -------------------------------------------------- */
    QTableWidget#glassTable, QTableView#glassTable {{ background: transparent; border: none; outline: none;
        gridline-color: transparent; selection-background-color: transparent; selection-color: {c['text']}; }}
    QTableWidget#glassTable::item {{ border: none; padding: 0 10px; background: transparent; }}
    QTableWidget#glassTable::item:selected {{ background: transparent; color: {c['text']}; }}
    QHeaderView {{ background: transparent; border: none; }}
    QHeaderView::section {{ background: transparent; color: {c['text_muted']}; border: none;
        border-bottom: 1px solid {c['separator']}; padding: 9px 10px 8px 10px; font-size: 11px; font-weight: 600; }}
    QTableWidget, QTreeWidget, QTableView, QTreeView {{ background-color: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border_soft']}; border-radius: 10px; gridline-color: {c['border_soft']};
        selection-background-color: {c['accent_soft']}; selection-color: {c['text']}; outline: none; }}
    QListWidget#nav, QListWidget#applicationList, QListWidget#plainList {{ background: transparent; border: none; outline: none; }}
    QListWidget#nav::item, QListWidget#applicationList::item, QListWidget#plainList::item {{ background: transparent; border: none; }}

    /* Scroll bars: slim overlay style ----------------------------------- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px 4px 0; }}
    QScrollBar::handle:vertical {{ background: {scroll_handle}; border-radius: 3px; min-height: 36px; margin: 0 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {scroll_handle_hover}; margin: 0 1px; border-radius: 4px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 4px 2px 4px; }}
    QScrollBar::handle:horizontal {{ background: {scroll_handle}; border-radius: 3px; min-width: 36px; margin: 2px 0; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: transparent; border: none; width: 0; height: 0; }}

    /* Stock controls kept consistent ------------------------------------ */
    QCheckBox, QRadioButton {{ spacing: 8px; padding: 3px 0; color: {c['text']}; background: transparent; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {control_line_hover}; border-radius: 5px;
        background-color: {c['fill_control']}; }}
    QCheckBox::indicator:checked {{ border-color: {c['button_fill']}; background-color: {c['button_fill']}; image: url("{check_icon}"); }}
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {c['text_muted']}; padding: 7px 12px; border: none; }}
    QTabBar::tab:selected {{ color: {c['text']}; font-weight: 600; }}
    QProgressBar {{ background-color: {c['fill_quiet']}; border: none; border-radius: 3px; height: 6px; text-align: center; }}
    QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 3px; }}
    QMenu {{ background-color: {c['glass_thick']}; color: {c['text']}; border: 1px solid {control_line};
        border-radius: 10px; padding: 5px; }}
    QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
    QMenu::item:selected {{ background-color: {c['button_fill']}; color: #FFFFFF; }}
    QMenu::item:disabled {{ color: {c['disabled']}; }}
    QMenu::separator {{ height: 1px; background-color: {c['separator']}; margin: 4px 8px; }}
    QMenuBar {{ background-color: transparent; color: {c['text']}; }}
    QToolTip {{ background-color: {c['tooltip']}; color: #FFFFFF; border: none; border-radius: 6px; padding: 5px 8px; }}
    QMessageBox QLabel {{ font-size: 13px; }}
    QStatusBar {{ background: transparent; color: {c['text_muted']}; border: none; }}
    QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {c['separator']}; }}
    """


def apply_typeface(app: QApplication, choice: str, restyle: bool = True) -> str:
    """Switch between Claude's type and the system font.

    Restyling touches every widget, so it happens only when the typeface actually
    changed, and callers about to apply the appearance anyway pass ``restyle=False``.
    Returns the typeface in use, which is ``System`` when Claude's fonts are not installed."""
    before = fonts.active()
    active = fonts.set_typeface(choice)
    font = app_font()
    if app.font() != font:
        app.setFont(font)
    if restyle and active != before:
        manager = getattr(app, "_jobhub_theme_manager", None)
        if manager is not None:
            manager.apply()
        else:
            app.setStyleSheet(stylesheet(app.property("jobhubResolvedAppearance") or None))
    return active


def apply_appearance(app: QApplication, appearance: str = "System") -> str:
    """Compatibility export for callers that import appearance from styles."""
    from .theme import apply_appearance as apply

    return apply(app, appearance)


def color_for_status(status: str, appearance: str | None = None) -> tuple[str, str]:
    """Return readable foreground and soft background colors for status labels."""
    scheme = appearance
    if scheme is None:
        app = QApplication.instance()
        if app is not None:
            scheme = app.property("jobhubResolvedAppearance") or None
    c = colors_for(scheme)
    return {
        "applied": (c["green"], c["green_soft"]),
        "needs_human": (c["amber"], c["amber_soft"]),
        "failed": (c["red"], c["red_soft"]),
        "queued": (c["info"], c["info_soft"]),
        "applying": (c["info"], c["info_soft"]),
        "dry_run": (c["indigo"], c["indigo_soft"]),
        # prepared applications
        "prepared": (c["info"], c["info_soft"]),
        "ready": (c["info"], c["info_soft"]),
        "needs_answers": (c["amber"], c["amber_soft"]),
        "approved": (c["indigo"], c["indigo_soft"]),
        "sending": (c["info"], c["info_soft"]),
        "sent": (c["green"], c["green_soft"]),
        "practice": (c["indigo"], c["indigo_soft"]),
        "unconfirmed": (c["amber"], c["amber_soft"]),
        "error": (c["red"], c["red_soft"]),
        # hiring stages
        "assessment": (c["indigo"], c["indigo_soft"]),
        "interview": (c["amber"], c["amber_soft"]),
        "offer": (c["green"], c["green_soft"]),
        "rejected": (c["red"], c["red_soft"]),
        "withdrawn": (c["text_muted"], c["fill_quiet"]),
    }.get(status, (c["text_muted"], c["fill_quiet"]))
