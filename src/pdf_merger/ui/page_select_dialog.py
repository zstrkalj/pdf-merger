"""Dialog for selecting which pages of a PDF to include in the merge."""

from __future__ import annotations

import re
from pathlib import Path

import fitz
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_THUMB_W   = 110   # dialog thumbnail width (px)
_GRID_COLS = 4     # columns in the thumbnail grid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_range(text: str, total: int) -> set[int]:
    """Parse a range string like ``"1-3, 5, 7"`` into 0-based page indices.

    Invalid tokens are silently skipped.
    """
    result: set[int] = set()
    for part in re.split(r"[,;\s]+", text.strip()):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo_s, hi_s = part.split("-", 1)
                lo = max(1, int(lo_s.strip()))
                hi = min(total, int(hi_s.strip()))
                if lo <= hi:
                    result.update(range(lo - 1, hi))
            else:
                n = int(part)
                if 1 <= n <= total:
                    result.add(n - 1)
        except ValueError:
            pass
    return result


def _selection_to_range_text(sel: set[int], total: int) -> str:
    """Convert a set of 0-based indices back to a compact range string like ``"1-3, 5, 7"``."""
    if not sel:
        return ""
    pages = sorted(sel)
    parts: list[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
        else:
            parts.append(f"{start + 1}-{prev + 1}" if start != prev else str(start + 1))
            start = prev = p
    parts.append(f"{start + 1}-{prev + 1}" if start != prev else str(start + 1))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Background worker — renders all pages of a PDF off the main thread
# ---------------------------------------------------------------------------

class _DialogThumbWorker(QThread):
    page_ready = pyqtSignal(int, bytes)  # 0-based page index, PNG bytes

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            doc = fitz.open(self._path)
            for i in range(doc.page_count):
                page = doc[i]
                scale = _THUMB_W / page.rect.width
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                self.page_ready.emit(i, bytes(pix.tobytes("png")))
            doc.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _PageThumb — a single clickable thumbnail tile
# ---------------------------------------------------------------------------

class _PageThumb(QFrame):
    """Displays one page thumbnail; clicking toggles its selection state."""

    toggled = pyqtSignal(int, bool)  # page_idx (0-based), new selected state

    _SEL_STYLE = (
        "QFrame { border: 3px solid #1976D2; border-radius: 4px; background: #E3F2FD; }"
    )
    _OFF_STYLE = (
        "QFrame { border: 2px solid #CCCCCC; border-radius: 4px; background: #FAFAFA; }"
    )

    def __init__(
        self, page_idx: int, selected: bool = True, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._page_idx = page_idx
        self._selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(_THUMB_W + 20)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Image area — placeholder until the worker delivers a pixmap
        self._img = QLabel("…")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setFixedWidth(_THUMB_W)
        self._img.setMinimumHeight(130)  # approximate portrait height
        layout.addWidget(self._img, alignment=Qt.AlignmentFlag.AlignHCenter)

        num = QLabel(str(page_idx + 1))
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont()
        f.setPointSize(9)
        num.setFont(f)
        layout.addWidget(num)

        self._update_style()

    # -- public API -------------------------------------------------------

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._img.setPixmap(pixmap)
        self._img.setMinimumHeight(0)

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._update_style()

    @property
    def selected(self) -> bool:
        return self._selected

    # -- internals --------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._selected = not self._selected
            self._update_style()
            self.toggled.emit(self._page_idx, self._selected)
        super().mousePressEvent(event)

    def _update_style(self) -> None:
        self.setStyleSheet(self._SEL_STYLE if self._selected else self._OFF_STYLE)


# ---------------------------------------------------------------------------
# PageSelectDialog — the full dialog
# ---------------------------------------------------------------------------

class PageSelectDialog(QDialog):
    """Scrollable grid of page thumbnails with range-text and bulk controls."""

    def __init__(
        self,
        path: Path,
        initial_selection: set[int] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Select Pages — {path.name}")
        self.resize(760, 580)
        self._path = path
        self._thumbs: list[_PageThumb] = []
        self._total = 0
        self._worker: _DialogThumbWorker | None = None
        self._build_ui()
        self._load(initial_selection)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def selected_pages(self) -> set[int] | None:
        """Return ``None`` when all pages are selected, else a set of 0-based indices."""
        sel = {i for i, t in enumerate(self._thumbs) if t.selected}
        if self._total > 0 and len(sel) == self._total:
            return None
        return sel

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── top controls ──────────────────────────────────────────────
        top = QHBoxLayout()

        top.addWidget(QLabel("Page range:"))

        self._range_edit = QLineEdit()
        self._range_edit.setPlaceholderText("e.g.  1-3, 5, 7")
        self._range_edit.setMaximumWidth(220)
        self._range_edit.returnPressed.connect(self._apply_range)
        top.addWidget(self._range_edit)

        apply_btn = QPushButton("Apply")
        apply_btn.setFixedWidth(64)
        apply_btn.clicked.connect(self._apply_range)
        top.addWidget(apply_btn)

        top.addSpacing(12)

        sel_all_btn   = QPushButton("Select All")
        desel_all_btn = QPushButton("Deselect All")
        sel_all_btn.clicked.connect(self._select_all)
        desel_all_btn.clicked.connect(self._deselect_all)
        top.addWidget(sel_all_btn)
        top.addWidget(desel_all_btn)

        top.addStretch()

        self._status = QLabel("0 of 0 selected")
        top.addWidget(self._status)

        root.addLayout(top)

        # ── thumbnail grid inside a scroll area ───────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(8)
        self._grid.setContentsMargins(8, 8, 8, 8)
        # Keep columns from stretching so thumbs stay left-aligned
        for col in range(_GRID_COLS):
            self._grid.setColumnStretch(col, 0)
        self._grid.setColumnStretch(_GRID_COLS, 1)  # trailing spacer column

        scroll.setWidget(self._grid_widget)
        root.addWidget(scroll)

        # ── dialog buttons ────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm")
        buttons.accepted.connect(self._on_confirm)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, initial_selection: set[int] | None) -> None:
        try:
            doc = fitz.open(str(self._path))
            self._total = doc.page_count
            doc.close()
        except Exception:
            self._total = 0
            return

        for i in range(self._total):
            selected = (initial_selection is None) or (i in initial_selection)
            thumb = _PageThumb(i, selected=selected)
            thumb.toggled.connect(self._on_thumb_toggled)
            self._thumbs.append(thumb)
            row, col = divmod(i, _GRID_COLS)
            self._grid.addWidget(thumb, row, col)

        # Pre-fill range text if we have an explicit (non-all) selection
        if initial_selection is not None:
            self._range_edit.setText(_selection_to_range_text(initial_selection, self._total))

        self._update_status()

        self._worker = _DialogThumbWorker(str(self._path))
        self._worker.page_ready.connect(self._on_page_ready)
        self._worker.start()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_page_ready(self, page_idx: int, png_bytes: bytes) -> None:
        if page_idx < len(self._thumbs):
            pixmap = QPixmap()
            pixmap.loadFromData(png_bytes)
            self._thumbs[page_idx].set_pixmap(pixmap)

    def _on_thumb_toggled(self, _idx: int, _sel: bool) -> None:
        self._update_status()

    def _apply_range(self) -> None:
        text = self._range_edit.text().strip()
        if not text:
            return
        indices = _parse_range(text, self._total)
        for i, thumb in enumerate(self._thumbs):
            thumb.set_selected(i in indices)
        self._update_status()

    def _select_all(self) -> None:
        for thumb in self._thumbs:
            thumb.set_selected(True)
        self._range_edit.clear()
        self._update_status()

    def _deselect_all(self) -> None:
        for thumb in self._thumbs:
            thumb.set_selected(False)
        self._range_edit.clear()
        self._update_status()

    def _on_confirm(self) -> None:
        count = sum(1 for t in self._thumbs if t.selected)
        if count == 0:
            QMessageBox.warning(
                self,
                "No Pages Selected",
                "Please select at least one page before confirming.",
            )
            return
        self.accept()

    def _update_status(self) -> None:
        count = sum(1 for t in self._thumbs if t.selected)
        self._status.setText(f"{count} of {self._total} selected")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(event)
