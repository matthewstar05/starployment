"""Custom-painted item views for jobs and applications.

Tables here are ``GlassTable`` instances: transparent, gridless, with a
rounded hover and selection highlight spanning the full row. Cells are drawn
by small delegates that read their data from item roles, so view code only
fills ``QTableWidgetItem``/``QListWidgetItem`` data and never builds per-row
widgets (which made 15-second refreshes slow and reset scroll state).
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QWidget,
)

from .format import posted_label, posted_within, status_label
from .styles import color_for_status
from .theme import current_colors
from .widgets import (
    item_state_hover,
    item_state_selected,
    paint_monogram,
    paint_pill,
    paint_star_mark,
    qcolor,
    rounded_path,
    ui_font,
    with_alpha,
)

SUBTITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 10
STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 11
SCORE_ROLE = int(Qt.ItemDataRole.UserRole) + 12
ACTION_ROLE = int(Qt.ItemDataRole.UserRole) + 13
SEARCH_ROLE = int(Qt.ItemDataRole.UserRole) + 14
STAR_ROLE = int(Qt.ItemDataRole.UserRole) + 15
POSTED_AT_ROLE = int(Qt.ItemDataRole.UserRole) + 17
RECORD_ROLE = int(Qt.ItemDataRole.UserRole)


class GlassTable(QTableWidget):
    """A gridless table with Apple-style full-row hover and selection."""

    def __init__(self, rows: int = 0, columns: int = 0, parent: QWidget | None = None):
        super().__init__(rows, columns, parent)
        self.setObjectName("glassTable")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setShowGrid(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(False)
        self.hover_row = -1
        self.hover_column = -1

    def mouseMoveEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        row, column = (index.row(), index.column()) if index.isValid() else (-1, -1)
        if (row, column) != (self.hover_row, self.hover_column):
            self.hover_row, self.hover_column = row, column
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_row = self.hover_column = -1
        self.viewport().update()
        super().leaveEvent(event)

    def _row_rect(self, row: int) -> QRectF:
        top = self.rowViewportPosition(row)
        return QRectF(4, top + 2, self.viewport().width() - 8, self.rowHeight(row) - 4)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = self.currentRow() if self.selectionModel() and self.selectionModel().hasSelection() else -1
        visible_rows = [row for row in range(self.rowCount()) if not self.isRowHidden(row)]
        painter.setPen(QPen(qcolor(colors["separator"]), 1))
        highlight_rows = {selected, self.hover_row}
        for position, row in enumerate(visible_rows[:-1]):
            next_row = visible_rows[position + 1]
            if row in highlight_rows or next_row in highlight_rows:
                continue
            y = self.rowViewportPosition(row) + self.rowHeight(row) - 0.5
            painter.drawLine(QPointF(16, y), QPointF(self.viewport().width() - 16, y))
        if 0 <= self.hover_row != selected and not self.isRowHidden(self.hover_row):
            painter.fillPath(rounded_path(self._row_rect(self.hover_row), 10), qcolor(colors["fill_hover"]))
        if 0 <= selected < self.rowCount() and not self.isRowHidden(selected):
            painter.fillPath(rounded_path(self._row_rect(selected), 10), qcolor(colors["fill_selected"]))
        painter.end()
        super().paintEvent(event)


def _cell_rect(option: QStyleOptionViewItem) -> QRectF:
    return QRectF(option.rect).adjusted(10, 0, -8, 0)


class RoleDelegate(QStyledItemDelegate):
    """Title on the first line, ``Company · Location`` beneath in secondary text.

    A starred role (``STAR_ROLE``) shows an amber star before its title.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        rect = _cell_rect(option)
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        painter.save()
        primary = ui_font(13, QFont.Weight.DemiBold)
        secondary = ui_font(12)
        painter.setFont(primary)
        painter.setPen(qcolor(colors["text"]))
        top = QRectF(rect.left(), rect.center().y() - 17, rect.width(), 18)
        if index.data(STAR_ROLE):
            paint_star_mark(painter, QPointF(top.left() + 6, top.center().y()), 6, colors["amber"])
            top.setLeft(top.left() + 17)
        painter.setPen(qcolor(colors["text"]))
        painter.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(primary).elidedText(title, Qt.TextElideMode.ElideRight, int(top.width())))
        painter.setFont(secondary)
        painter.setPen(qcolor(colors["text_muted"]))
        bottom = QRectF(rect.left(), rect.center().y() + 1, rect.width(), 17)
        painter.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(secondary).elidedText(subtitle, Qt.TextElideMode.ElideRight, int(rect.width())))
        painter.restore()


class TextDelegate(QStyledItemDelegate):
    """Single-line secondary text (salary and similar columns)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.save()
        font = ui_font(13)
        painter.setFont(font)
        painter.setPen(qcolor(colors["text_muted"] if text == "—" else colors["text"]))
        rect = _cell_rect(option)
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width())))
        painter.restore()


class PostedDelegate(QStyledItemDelegate):
    """When a role was posted: the date (and time when known), then how long ago.

    The age is worked out at paint time from ``POSTED_AT_ROLE``, so it stays
    current without rebuilding the table. Postings from the last 24 hours show
    their age in green.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        rect = _cell_rect(option)
        posted_at = index.data(POSTED_AT_ROLE)
        when, ago = posted_label(posted_at)
        painter.save()
        primary, secondary = ui_font(13), ui_font(12)
        width = int(rect.width())
        if not ago:
            painter.setFont(primary)
            painter.setPen(qcolor(colors["text_muted"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, when or "—")
            painter.restore()
            return
        painter.setFont(primary)
        painter.setPen(qcolor(colors["text"]))
        painter.drawText(QRectF(rect.left(), rect.center().y() - 17, rect.width(), 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(primary).elidedText(when, Qt.TextElideMode.ElideRight, width))
        painter.setFont(secondary)
        painter.setPen(qcolor(colors["green"] if posted_within(posted_at, 24) else colors["text_muted"]))
        painter.drawText(QRectF(rect.left(), rect.center().y() + 1, rect.width(), 17),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(secondary).elidedText(ago, Qt.TextElideMode.ElideRight, width))
        painter.restore()


def score_color(score: float) -> str:
    colors = current_colors()
    if score >= 75:
        return colors["green"]
    if score >= 55:
        return colors["info"]   # blue, so the bands stay distinct from the gold highlight and amber
    if score >= 35:
        return colors["amber"]
    return colors["text_subtle"]


class ScoreDelegate(QStyledItemDelegate):
    """An activity-ring style match score."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        score = float(index.data(SCORE_ROLE) or 0.0)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        diameter = 30.0
        rect = QRectF(option.rect.left() + 12, option.rect.center().y() - diameter / 2 + 0.5, diameter, diameter)
        ring = rect.adjusted(2, 2, -2, -2)
        track = QPen(qcolor(colors["fill_quiet"]), 3.2)
        painter.setPen(track)
        painter.drawEllipse(ring)
        progress = QPen(qcolor(score_color(score)), 3.2)
        progress.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(progress)
        span = int(-360 * 16 * max(0.0, min(100.0, score)) / 100.0)
        if span:
            painter.drawArc(ring, 90 * 16, span)
        painter.setPen(qcolor(colors["text"]))
        painter.setFont(ui_font(11, QFont.Weight.DemiBold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{score:.0f}")
        painter.restore()


class StatusDelegate(QStyledItemDelegate):
    """A tinted status capsule."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        status = str(index.data(STATUS_ROLE) or "")
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or status_label(status))
        fg, bg = color_for_status(status)
        font = ui_font(11, QFont.Weight.DemiBold)
        width = QFontMetrics(font).horizontalAdvance(text) + 18
        rect = QRectF(option.rect.left() + 10, option.rect.center().y() - 10, width, 20)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_pill(painter, rect, text, fg, bg, font)
        painter.restore()


class ActionDelegate(QStyledItemDelegate):
    """A capsule action drawn in the cell; the view handles clicks via ``cellClicked``."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        kind = str(index.data(ACTION_ROLE) or "view")
        view = option.widget
        hovered = isinstance(view, GlassTable) and view.hover_row == index.row() and view.hover_column == index.column()
        fg, bg = {
            "apply": (colors["accent_text"], colors["accent_soft"]),
            "retry": (colors["amber"], colors["amber_soft"]),
        }.get(kind, (colors["text"], colors["fill_quiet"]))
        if hovered and kind == "apply":
            fg, bg = "#FFFFFF", colors["button_fill"]
        elif hovered:
            bg = with_alpha(bg, min(1.0, qcolor(bg).alphaF() + 0.18))
        font = ui_font(12, QFont.Weight.DemiBold)
        width = max(58.0, QFontMetrics(font).horizontalAdvance(text) + 24.0)
        rect = QRectF(option.rect.right() - width - 12, option.rect.center().y() - 12, width, 24)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_pill(painter, rect, text, fg, bg, font)
        painter.restore()


class ApplicationDelegate(QStyledItemDelegate):
    """A two-line application row: monogram, employer, role, status, and time."""

    HEIGHT = 64

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(max(1, option.rect.width()), self.HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        record = index.data(RECORD_ROLE) or {}
        company = str(record.get("company") or "Employer")
        title = str(record.get("title") or "Role")
        status = str(record.get("status") or "unknown")
        detail = str(index.data(SUBTITLE_ROLE) or "")
        when = str(index.data(SEARCH_ROLE) or "")
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if item_state_selected(option):
            painter.fillPath(rounded_path(rect, 12), qcolor(colors["fill_selected"]))
        elif item_state_hover(option):
            painter.fillPath(rounded_path(rect, 12), qcolor(colors["fill_hover"]))
        paint_monogram(painter, QRectF(rect.left() + 10, rect.center().y() - 17, 34, 34), company)
        fg, bg = color_for_status(status)
        pill_font = ui_font(10, QFont.Weight.DemiBold)
        pill_text = status_label(status)
        pill_width = QFontMetrics(pill_font).horizontalAdvance(pill_text) + 14
        pill = QRectF(rect.right() - pill_width - 10, rect.top() + 12, pill_width, 18)
        paint_pill(painter, pill, pill_text, fg, bg, pill_font)
        text_left = rect.left() + 56
        primary = ui_font(13, QFont.Weight.DemiBold)
        painter.setFont(primary)
        painter.setPen(qcolor(colors["text"]))
        starred = bool(record.get("_starred"))
        width = pill.left() - text_left - 8 - (16 if starred else 0)
        shown = QFontMetrics(primary).elidedText(company, Qt.TextElideMode.ElideRight, int(width))
        painter.drawText(QRectF(text_left, rect.top() + 11, width, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         shown)
        if starred:
            star_x = text_left + QFontMetrics(primary).horizontalAdvance(shown) + 10
            paint_star_mark(painter, QPointF(star_x, rect.top() + 21), 5.5, colors["amber"])
        secondary = ui_font(12)
        painter.setFont(secondary)
        painter.setPen(qcolor(colors["text_muted"]))
        line = title + (f" · {detail}" if detail else "")
        time_width = QFontMetrics(secondary).horizontalAdvance(when) + 4 if when else 0
        second_width = rect.right() - text_left - 10 - time_width - 8
        painter.drawText(QRectF(text_left, rect.top() + 32, second_width, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(secondary).elidedText(line, Qt.TextElideMode.ElideRight, int(second_width)))
        if when:
            painter.setPen(qcolor(colors["text_subtle"]))
            painter.drawText(QRectF(rect.right() - time_width - 10, rect.top() + 32, time_width, 18),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, when)
        painter.restore()


def configure_columns(table: QTableWidget, stretch: int, fixed: dict[int, int]) -> None:
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(40)
    header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    for column, width in fixed.items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)


class TwoLineDelegate(QStyledItemDelegate):
    """A plain two-line list row: bold first line, muted second line, rounded highlight."""

    HEIGHT = 54

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(max(1, option.rect.width()), self.HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        colors = current_colors()
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        first, _, second = text.partition("\n")
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if item_state_selected(option):
            painter.fillPath(rounded_path(rect, 10), qcolor(colors["fill_selected"]))
        elif item_state_hover(option):
            painter.fillPath(rounded_path(rect, 10), qcolor(colors["fill_hover"]))
        width = int(rect.width() - 24)
        primary = ui_font(13, QFont.Weight.DemiBold)
        painter.setFont(primary)
        painter.setPen(qcolor(colors["text"]))
        painter.drawText(QRectF(rect.left() + 12, rect.top() + 8, width, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(primary).elidedText(first, Qt.TextElideMode.ElideRight, width))
        if second:
            secondary = ui_font(12)
            painter.setFont(secondary)
            painter.setPen(qcolor(colors["text_muted"]))
            painter.drawText(QRectF(rect.left() + 12, rect.top() + 27, width, 17),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             QFontMetrics(secondary).elidedText(second, Qt.TextElideMode.ElideRight, width))
        painter.restore()
