"""Structured editors that replace typed syntax in setup and settings.

* ``TagEditor``: a list of short values (titles, places, skills, board names)
  shown as removable chips with an inline "Add" field.
* ``KeyValueEditor``: labeled facts or question/answer pairs as table rows with
  a label picker (with suggestions) and a value field, instead of
  ``label | value`` lines.
* ``BulletEditor``: resume bullets as separate, auto-growing text rows.

Each editor emits ``changed`` on any edit so ``DirtyTracker`` can watch it.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from .icons import icon_pixmap, jobhub_icon
from .theme import current_colors
from .widgets import SegmentedControl, button, label, qcolor, rounded_path, ui_font


class FlowLayout(QLayout):
    """Lays children out left to right, wrapping onto new lines (for chips)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self._items.insert(index, QWidgetItem(widget))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line > 0:
                x, y, line = area.x(), y + line + self._spacing, 0
            if apply:
                width = min(hint.width(), area.width()) if item.widget() is not None else hint.width()
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()


class Chip(QWidget):
    """A removable value capsule, painted (no native material per chip)."""

    removed = Signal(str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.text = text
        self._hover_x = False
        self.setMouseTracking(True)
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _font(self) -> QFont:
        return ui_font(12, QFont.Weight.Medium)

    def sizeHint(self) -> QSize:
        width = QFontMetrics(self._font()).horizontalAdvance(self.text)
        return QSize(min(360, width + 12 + 30), 26)

    def _close_rect(self) -> QRectF:
        return QRectF(self.width() - 22, (self.height() - 16) / 2, 16, 16)

    def mouseMoveEvent(self, event) -> None:
        hover = self._close_rect().adjusted(-3, -3, 3, 3).contains(event.position())
        if hover != self._hover_x:
            self._hover_x = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover_x = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._close_rect().adjusted(-3, -3, 3, 3).contains(event.position()):
            self.removed.emit(self.text)

    def paintEvent(self, event) -> None:
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillPath(rounded_path(rect, rect.height() / 2), qcolor(colors["accent_soft"]))
        painter.setFont(self._font())
        painter.setPen(qcolor(colors["accent_text"]))
        text_rect = QRectF(12, 0, self.width() - 12 - 28, self.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(self._font()).elidedText(self.text, Qt.TextElideMode.ElideRight, int(text_rect.width())))
        close = self._close_rect()
        if self._hover_x:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(colors["fill_quiet"]))
            painter.drawEllipse(close)
        painter.drawPixmap(close.center() - QRectF(0, 0, 10, 10).center(),
                           icon_pixmap("close", colors["accent_text"], 10, 2.4))
        painter.end()


class TagEditor(QWidget):
    """A list of short values as chips. Type and press Return (or comma) to add."""

    changed = Signal()

    def __init__(self, placeholder: str = "Add…", suggestions: list[str] | None = None,
                 parent: QWidget | None = None, *, lowercase_unique: bool = True):
        super().__init__(parent)
        self.setProperty("formExpand", True)
        self._values: list[str] = []
        self._unique_key = (lambda s: s.casefold()) if lowercase_unique else (lambda s: s)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.flow = FlowLayout(self, spacing=6)
        self.input = QLineEdit()
        self.input.setObjectName("tagInput")
        self.input.setPlaceholderText(placeholder)
        self.input.setAccessibleName(placeholder)
        self.input.setMinimumWidth(170)
        self.input.setFixedHeight(26)
        self.input.returnPressed.connect(self._commit)
        self.input.textEdited.connect(self._maybe_split)
        if suggestions:
            completer = QCompleter(sorted(set(suggestions)), self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.activated.connect(lambda text: (self.input.setText(text), self._commit()))
            self.input.setCompleter(completer)
        self.flow.addWidget(self.input)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.flow.heightForWidth(width)

    def sizeHint(self) -> QSize:
        return QSize(320, self.heightForWidth(max(320, self.width())))

    def values(self) -> list[str]:
        pending = self.input.text().strip()
        extra = [pending] if pending and self._unique_key(pending) not in {self._unique_key(v) for v in self._values} else []
        return list(self._values) + extra

    def set_values(self, values: list[str]) -> None:
        for chip in self.findChildren(Chip):
            self.flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self._values = []
        for value in values:
            self._add(str(value), emit=False)
        self.input.clear()
        self.updateGeometry()

    def _add(self, value: str, emit: bool = True) -> None:
        value = " ".join(value.split())
        if not value or self._unique_key(value) in {self._unique_key(v) for v in self._values}:
            return
        self._values.append(value)
        chip = Chip(value)
        chip.removed.connect(self._remove)
        self.flow.insertWidget(self.flow.count() - 1, chip)
        chip.show()
        self.updateGeometry()
        if emit:
            self.changed.emit()

    def _remove(self, value: str) -> None:
        if value not in self._values:
            return
        self._values.remove(value)
        for chip in self.findChildren(Chip):
            if chip.text == value:
                self.flow.removeWidget(chip)
                chip.hide()
                chip.deleteLater()
                break
        self.updateGeometry()
        self.changed.emit()

    def _commit(self) -> None:
        text = self.input.text()
        self.input.clear()
        for part in text.split(","):
            self._add(part)

    def _maybe_split(self, text: str) -> None:
        if "," in text or "\n" in text:
            self._commit()


SUGGESTED_FACTS = [
    "Home city", "Preferred name", "Pronunciation of name", "Notice period", "Earliest start date",
    "Desired work arrangement", "Graduation date", "Citizenship", "Languages spoken", "Portfolio",
    "Personal website", "Twitter / X", "Clearance status", "Time zone", "Hours available per week",
]
SUGGESTED_QUESTIONS = [
    "Why do you want to work here?", "Why are you interested in this role?", "Tell us about yourself",
    "What are you looking for in your next role?", "Describe a project you are proud of",
    "Are you willing to work in person?", "Do you have experience with Python?",
    "What is your expected graduation date?", "Have you previously worked for this company?",
    "Do you know anyone who works here?", "Cover letter",
]


class _KeyValueRow(QWidget):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, key: str, value: str, suggestions: list[str], multiline: bool,
                 key_placeholder: str, value_placeholder: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("formRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.key.addItems(suggestions)
        self.key.setCurrentText(key)
        self.key.lineEdit().setPlaceholderText(key_placeholder)
        self.key.setMinimumWidth(170)
        self.key.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.key.setAccessibleName(key_placeholder)
        self.key.currentTextChanged.connect(self.changed)
        if multiline:
            self.value = QPlainTextEdit(value)
            self.value.setFixedHeight(58)
            self.value.setTabChangesFocus(True)
            self.value.textChanged.connect(self.changed)
        else:
            self.value = QLineEdit(value)
            self.value.textChanged.connect(self.changed)
        self.value.setPlaceholderText(value_placeholder)
        self.value.setAccessibleName(value_placeholder)
        remove = QToolButton()
        remove.setObjectName("rowRemove")
        remove.setIcon(jobhub_icon("close", current_colors()["text_muted"], 12))
        remove.setToolTip("Remove")
        remove.setAccessibleName("Remove row")
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(self.key, 2, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.value, 3)
        row.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)

    def pair(self) -> tuple[str, str]:
        value = self.value.toPlainText() if isinstance(self.value, QPlainTextEdit) else self.value.text()
        return self.key.currentText().strip(), value.strip()


class KeyValueEditor(QWidget):
    """Rows of ``label → value`` with an editable, suggested label and a remove button."""

    changed = Signal()

    def __init__(self, *, suggestions: list[str] | None = None, multiline: bool = False,
                 key_placeholder: str = "Label", value_placeholder: str = "Value",
                 add_text: str = "Add", parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("formExpand", True)
        self._suggestions = list(suggestions or [])
        self._multiline = multiline
        self._placeholders = (key_placeholder, value_placeholder)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(8)
        column.addLayout(self.rows_layout)
        self.empty = label("Nothing added yet.", "rowNote")
        column.addWidget(self.empty)
        actions = QHBoxLayout()
        self.add_button = button(add_text, "quiet", icon="plus")
        self.add_button.clicked.connect(lambda: self.add_row("", "", focus=True))
        actions.addWidget(self.add_button)
        actions.addStretch(1)
        column.addLayout(actions)
        self.rows: list[_KeyValueRow] = []

    def add_row(self, key: str = "", value: str = "", *, focus: bool = False) -> _KeyValueRow:
        row = _KeyValueRow(key, value, self._suggestions, self._multiline, *self._placeholders)
        row.changed.connect(self.changed)
        row.removed.connect(self._remove)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        self.empty.hide()
        if focus:
            row.key.setFocus()
            self.changed.emit()
        return row

    def _remove(self, row: _KeyValueRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            self.rows_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
            self.empty.setVisible(not self.rows)
            self.changed.emit()

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in self.rows:
            key, value = row.pair()
            if key and value:
                out[key] = value
        return out

    def keys_with_blank_values(self) -> list[str]:
        return [row.pair()[0] for row in self.rows if row.pair()[0] and not row.pair()[1]]

    def set_values(self, values: dict[str, object]) -> None:
        for row in list(self.rows):
            self.rows_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.rows = []
        for key, value in (values or {}).items():
            if isinstance(value, bool):
                value = "Yes" if value else "No"
            self.add_row(str(key), str(value))
        self.empty.setVisible(not self.rows)


class _AutoText(QPlainTextEdit):
    """A plain-text field that grows to fit its wrapped content."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._fit()

    def _fit(self, *_args) -> None:
        lines = max(1, int(self.document().documentLayout().documentSize().height()))
        line_height = self.fontMetrics().lineSpacing()
        self.setFixedHeight(max(34, min(160, lines * line_height + 16)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()


class BulletEditor(QWidget):
    """Resume bullets as separate rows: each grows with its text and can be removed or reordered."""

    changed = Signal()

    def __init__(self, placeholder: str = "Describe what you did and the result", parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("formExpand", True)
        self._placeholder = placeholder
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(6)
        column.addLayout(self.rows_layout)
        add = button("Add bullet", "quiet", icon="plus")
        add.clicked.connect(lambda: self._add("", focus=True))
        row = QHBoxLayout()
        row.addWidget(add)
        row.addStretch(1)
        column.addLayout(row)
        self.fields: list[_AutoText] = []

    def _add(self, text: str, focus: bool = False) -> None:
        holder = QWidget()
        holder.setObjectName("formRow")
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        dot = label("•", "rowNote", wrap=False)
        dot.setFixedWidth(10)
        field = _AutoText(text)
        field.setPlaceholderText(self._placeholder)
        field.setAccessibleName("Bullet")
        field.textChanged.connect(self.changed)
        up = QToolButton()
        up.setObjectName("rowRemove")
        up.setIcon(jobhub_icon("chevron-up", current_colors()["text_muted"], 12))
        up.setToolTip("Move up")
        up.clicked.connect(lambda: self._move(field, -1))
        remove = QToolButton()
        remove.setObjectName("rowRemove")
        remove.setIcon(jobhub_icon("close", current_colors()["text_muted"], 12))
        remove.setToolTip("Remove bullet")
        remove.clicked.connect(lambda: self._remove(holder, field))
        line.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(field, 1)
        line.addWidget(up, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(remove, 0, Qt.AlignmentFlag.AlignTop)
        self.fields.append(field)
        self.rows_layout.addWidget(holder)
        if focus:
            field.setFocus()
            self.changed.emit()

    def _remove(self, holder: QWidget, field: _AutoText) -> None:
        if field in self.fields:
            self.fields.remove(field)
        self.rows_layout.removeWidget(holder)
        holder.hide()
        holder.deleteLater()
        self.changed.emit()

    def _move(self, field: _AutoText, step: int) -> None:
        index = self.fields.index(field)
        target = index + step
        if not 0 <= target < len(self.fields):
            return
        texts = self.values(keep_blank=True)
        texts[index], texts[target] = texts[target], texts[index]
        self.set_values(texts)
        self.fields[target].setFocus()
        self.changed.emit()

    def values(self, keep_blank: bool = False) -> list[str]:
        texts = [" ".join(field.toPlainText().split()) for field in self.fields]
        return texts if keep_blank else [t for t in texts if t]

    def set_values(self, bullets: list[str]) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.fields = []
        for text in bullets:
            self._add(text)


def yes_no_control() -> SegmentedControl:
    """A three-way Yes / No / Not set choice for eligibility answers."""
    control = SegmentedControl(compact=True)
    control.addItem("Not set", "")
    control.addItem("Yes", "Yes")
    control.addItem("No", "No")
    return control
