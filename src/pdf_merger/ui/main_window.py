"""Main application window."""

from __future__ import annotations

from pathlib import Path

import fitz  # pymupdf
from PyQt6.QtCore import QSettings, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from pypdf import PdfReader

from pdf_merger.core.merger import PdfMerger, PdfMergerError
from pdf_merger.ui.page_select_dialog import PageSelectDialog

_APP_NAME = "Sal Consulting PDF Merger App"
_APP_ORG  = "Sal Consulting"
_APP_VER  = "1.0"

# Thumbnail target width (px). Height is proportional to each PDF's aspect ratio.
_THUMB_W = 52
# Row height must comfortably hold the tallest thumbnail (portrait Letter ≈ 68px).
_ROW_H = 74


def _fmt_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024  # type: ignore[assignment]
    return f"{n_bytes:.1f} TB"


# ---------------------------------------------------------------------------
# Drop-aware table
# ---------------------------------------------------------------------------

class _PdfDropTable(QTableWidget):
    """QTableWidget that accepts PDF file drops from the OS file manager."""

    files_dropped = pyqtSignal(list)  # list[str] — only .pdf paths

    def __init__(self, rows: int, cols: int, parent: QWidget | None = None) -> None:
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls() and self._pdf_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls() and self._pdf_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        paths = self._pdf_paths(event.mimeData())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _pdf_paths(mime) -> list[str]:
        return [
            u.toLocalFile()
            for u in mime.urls()
            if u.toLocalFile().lower().endswith(".pdf")
        ]


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _ThumbnailWorker(QThread):
    """Renders first-page thumbnails for a batch of PDF paths off the main thread."""

    thumbnail_ready = pyqtSignal(str, bytes)  # path, PNG bytes

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:
        for path in self._paths:
            try:
                doc = fitz.open(path)
                if doc.page_count == 0:
                    doc.close()
                    continue
                page = doc[0]
                scale = _THUMB_W / page.rect.width
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                self.thumbnail_ready.emit(path, bytes(pix.tobytes("png")))
                doc.close()
            except Exception:
                pass  # silently skip files that can't be rendered


class _MergeWorker(QThread):
    progress = pyqtSignal(int, int)  # current (1-based), total
    finished = pyqtSignal(str)       # resolved output path
    errored  = pyqtSignal(str)       # error message

    def __init__(
        self,
        paths: list[str],
        output_path: str,
        page_selections: dict[str, set[int] | None],
    ) -> None:
        super().__init__()
        self._paths = paths
        self._output_path = output_path
        # Build a parallel list: sorted page indices or None (= all pages)
        self._pages_per_file: list[list[int] | None] = [
            sorted(page_selections[p]) if page_selections.get(p) is not None else None
            for p in paths
        ]

    def run(self) -> None:
        try:
            merger = PdfMerger(
                self._paths,
                progress_callback=lambda cur, tot: self.progress.emit(cur, tot),
                pages_per_file=self._pages_per_file,
            )
            result = merger.merge(self._output_path)
            self.finished.emit(str(result))
        except PdfMergerError as exc:
            self.errored.emit(str(exc))
        except Exception as exc:
            self.errored.emit(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    _COL_NUM   = 0
    _COL_NAME  = 1
    _COL_PAGES = 2
    _COL_SIZE  = 3

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(_APP_NAME)
        self.setMinimumSize(700, 500)
        self.resize(760, 540)

        self._settings = QSettings(_APP_ORG, "PdfMerger")
        self._merge_worker: _MergeWorker | None = None
        self._thumb_worker: _ThumbnailWorker | None = None
        self._thumb_queue: list[str] = []
        # page selections: None means "all pages"; set[int] means specific 0-based indices
        self._page_selections: dict[str, set[int] | None] = {}
        self._page_counts: dict[str, int] = {}

        self._build_menu()
        self._build_ui()
        self._build_shortcuts()
        self._update_status()

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        mb = self.menuBar()

        # ── File ─────────────────────────────────────────────────────
        file_menu = mb.addMenu("&File")

        add_action = QAction("Add Files…", self)
        add_action.setShortcut(QKeySequence("Ctrl+O"))
        add_action.setStatusTip("Open PDF files to add to the list")
        add_action.triggered.connect(self._add_files)
        file_menu.addAction(add_action)

        merge_action = QAction("Merge PDFs", self)
        merge_action.setShortcut(QKeySequence("Ctrl+M"))
        merge_action.setStatusTip("Merge all listed PDFs into one file")
        merge_action.triggered.connect(self._start_merge)
        file_menu.addAction(merge_action)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.setStatusTip("Exit the application")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # ── Help ─────────────────────────────────────────────────────
        help_menu = mb.addMenu("&Help")

        about_action = QAction("About", self)
        about_action.setStatusTip(f"About {_APP_NAME}")
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------
    # Keyboard shortcuts not covered by menu actions
    # ------------------------------------------------------------------

    def _build_shortcuts(self) -> None:
        delete_sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        delete_sc.activated.connect(self._remove_selected)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(0)

        # ── header banner ─────────────────────────────────────────────
        header = QLabel()
        header.setText(
            '<span style="font-size:15pt;">'
            '<b>Sal Consulting</b> PDF Merger App'
            "</span>"
        )
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet(
            "QLabel {"
            "  background-color: #1565C0;"
            "  color: white;"
            "  padding: 14px 16px;"
            "}"
        )
        root.addWidget(header)

        # ── content area (with its own margins) ───────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 10, 12, 0)
        content_layout.setSpacing(8)
        root.addWidget(content)

        # ── top button row ────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self._add_btn    = QPushButton("Add Files")
        self._remove_btn = QPushButton("Remove")
        self._up_btn     = QPushButton("Move Up")
        self._down_btn   = QPushButton("Move Down")

        self._add_btn.setToolTip("Add PDF files  (Ctrl+O)")
        self._remove_btn.setToolTip("Remove selected files  (Delete)")
        self._up_btn.setToolTip("Move selected row up")
        self._down_btn.setToolTip("Move selected row down")

        self._add_btn.clicked.connect(self._add_files)
        self._remove_btn.clicked.connect(self._remove_selected)
        self._up_btn.clicked.connect(self._move_up)
        self._down_btn.clicked.connect(self._move_down)

        for btn in (self._add_btn, self._remove_btn, self._up_btn, self._down_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch()

        hint = QLabel("Double-click a row to select pages")
        hint.setStyleSheet("color: #757575; font-size: 9pt;")
        btn_row.addWidget(hint)

        content_layout.addLayout(btn_row)

        # ── file table ────────────────────────────────────────────────
        self._table = _PdfDropTable(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "Filename", "Pages", "Size"])

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self._COL_NUM,   QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self._COL_NAME,  QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self._COL_PAGES, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self._COL_SIZE,  QHeaderView.ResizeMode.ResizeToContents)

        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_H)
        self._table.setIconSize(QSize(_THUMB_W, _ROW_H - 4))
        self._table.setShowGrid(False)

        self._table.files_dropped.connect(self._on_files_dropped)
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        content_layout.addWidget(self._table)

        # ── progress bar ──────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setFixedHeight(18)
        self._progress.setVisible(False)
        content_layout.addWidget(self._progress)

        # ── merge button ──────────────────────────────────────────────
        self._merge_btn = QPushButton("Merge PDFs  (Ctrl+M)")
        self._merge_btn.setFixedHeight(38)
        self._merge_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #1565C0;"
            "  color: white;"
            "  font-weight: bold;"
            "  font-size: 10pt;"
            "  border-radius: 4px;"
            "  padding: 6px 16px;"
            "}"
            "QPushButton:hover  { background-color: #1976D2; }"
            "QPushButton:pressed { background-color: #0D47A1; }"
            "QPushButton:disabled { background-color: #9E9E9E; }"
        )
        self._merge_btn.clicked.connect(self._start_merge)
        content_layout.addWidget(self._merge_btn)

        # Status bar
        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------
    # File list management
    # ------------------------------------------------------------------

    def _add_files(self) -> None:
        last_dir = str(self._settings.value("last_dir", ""))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDF Files", last_dir, "PDF Files (*.pdf)"
        )
        if not paths:
            return
        self._settings.setValue("last_dir", str(Path(paths[0]).parent))
        skipped = 0
        for path in paths:
            before = self._table.rowCount()
            self._append_row(Path(path))
            if self._table.rowCount() == before:
                skipped += 1
        if skipped:
            QMessageBox.information(
                self,
                "Duplicates Skipped",
                f"{skipped} file(s) were already in the list and were skipped.",
            )
        self._update_status()

    def _on_files_dropped(self, paths: list[str]) -> None:
        for path in paths:
            self._append_row(Path(path))
        self._update_status()

    def _append_row(self, path: Path) -> None:
        # Skip duplicates silently
        for row in range(self._table.rowCount()):
            if self._path_at(row) == path:
                return

        if not path.exists():
            QMessageBox.warning(
                self,
                "File Not Found",
                f"The following file could not be found and was not added:\n\n{path}",
            )
            return

        try:
            total_pages = len(PdfReader(str(path)).pages)
            pages = str(total_pages)
        except Exception:
            QMessageBox.warning(
                self,
                "Invalid PDF",
                f"The following file does not appear to be a valid PDF and was skipped:\n\n{path}",
            )
            return

        self._page_counts[str(path)] = total_pages
        self._page_selections.setdefault(str(path), None)

        size = _fmt_size(path.stat().st_size)
        row  = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, _ROW_H)

        num_item = QTableWidgetItem(str(row + 1))
        num_item.setData(Qt.ItemDataRole.UserRole, str(path))
        num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        name_item = QTableWidgetItem(path.name)
        name_item.setToolTip(str(path))

        pages_item = QTableWidgetItem(pages)
        pages_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        size_item = QTableWidgetItem(size)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        self._table.setItem(row, self._COL_NUM,   num_item)
        self._table.setItem(row, self._COL_NAME,  name_item)
        self._table.setItem(row, self._COL_PAGES, pages_item)
        self._table.setItem(row, self._COL_SIZE,  size_item)

        self._queue_thumbnail(str(path))

    def _path_at(self, row: int) -> Path:
        return Path(self._table.item(row, self._COL_NUM).data(Qt.ItemDataRole.UserRole))

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        path = self._path_at(row)
        current = self._page_selections.get(str(path))
        dlg = PageSelectDialog(path, current, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._page_selections[str(path)] = dlg.selected_pages()
            self._update_row_label(row)

    def _update_row_label(self, row: int) -> None:
        path = self._path_at(row)
        item = self._table.item(row, self._COL_NAME)
        if item is None:
            return
        sel = self._page_selections.get(str(path))
        if sel is None:
            item.setText(path.name)
        else:
            total = self._page_counts.get(str(path), 0)
            item.setText(f"{path.name} — {len(sel)} of {total} pages selected")

    def _remove_selected(self) -> None:
        rows = sorted(
            {idx.row() for idx in self._table.selectedIndexes()},
            reverse=True,
        )
        if not rows:
            return
        for row in rows:
            path_str = str(self._path_at(row))
            self._page_selections.pop(path_str, None)
            self._page_counts.pop(path_str, None)
            self._table.removeRow(row)
        self._renumber()
        self._update_status()

    def _move_up(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        if not rows or rows[0] == 0:
            return
        for row in rows:
            self._swap_rows(row - 1, row)
        self._select_rows([r - 1 for r in rows])
        self._renumber()

    def _move_down(self) -> None:
        rows = sorted(
            {idx.row() for idx in self._table.selectedIndexes()},
            reverse=True,
        )
        if not rows or rows[0] == self._table.rowCount() - 1:
            return
        for row in rows:
            self._swap_rows(row, row + 1)
        self._select_rows([r + 1 for r in rows])
        self._renumber()

    def _swap_rows(self, a: int, b: int) -> None:
        for col in range(self._table.columnCount()):
            item_a = self._table.takeItem(a, col)
            item_b = self._table.takeItem(b, col)
            self._table.setItem(a, col, item_b)
            self._table.setItem(b, col, item_a)

    def _select_rows(self, rows: list[int]) -> None:
        self._table.clearSelection()
        for row in rows:
            for col in range(self._table.columnCount()):
                item = self._table.item(row, col)
                if item:
                    item.setSelected(True)

    def _renumber(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NUM)
            if item:
                item.setText(str(row + 1))

    # ------------------------------------------------------------------
    # Thumbnail rendering (background thread → main thread via signal)
    # ------------------------------------------------------------------

    def _queue_thumbnail(self, path: str) -> None:
        self._thumb_queue.append(path)
        self._flush_thumb_queue()

    def _flush_thumb_queue(self) -> None:
        if self._thumb_worker is not None and self._thumb_worker.isRunning():
            return
        if not self._thumb_queue:
            return
        batch, self._thumb_queue = self._thumb_queue[:], []
        self._thumb_worker = _ThumbnailWorker(batch)
        self._thumb_worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumb_worker.finished.connect(self._flush_thumb_queue)
        self._thumb_worker.start()

    def _on_thumbnail_ready(self, path: str, png_bytes: bytes) -> None:
        target = Path(path)
        for row in range(self._table.rowCount()):
            if self._path_at(row) == target:
                pixmap = QPixmap()
                pixmap.loadFromData(png_bytes)
                item = self._table.item(row, self._COL_NAME)
                if item:
                    item.setIcon(QIcon(pixmap))
                break

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _start_merge(self) -> None:
        if self._table.rowCount() == 0:
            QMessageBox.warning(
                self,
                "No Files",
                "Please add at least one PDF file before merging.\n\n"
                "Use Add Files or drag PDF files into the list.",
            )
            return

        last_dir = str(self._settings.value("last_dir", ""))
        default_path = str(Path(last_dir) / "merged.pdf") if last_dir else "merged.pdf"
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Merged PDF", default_path, "PDF Files (*.pdf)"
        )
        if not output_path:
            return
        self._settings.setValue("last_dir", str(Path(output_path).parent))

        paths = [str(self._path_at(row)) for row in range(self._table.rowCount())]

        self._progress.setMaximum(len(paths))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._set_controls_enabled(False)
        self.statusBar().showMessage("Merging…")

        self._merge_worker = _MergeWorker(paths, output_path, self._page_selections)
        self._merge_worker.progress.connect(self._on_progress)
        self._merge_worker.finished.connect(self._on_finished)
        self._merge_worker.errored.connect(self._on_error)
        self._merge_worker.start()

    def _on_progress(self, current: int, total: int) -> None:
        self._progress.setValue(current)
        self.statusBar().showMessage(f"Merging… {current} of {total} files processed")

    def _on_finished(self, output_path: str) -> None:
        self._progress.setVisible(False)
        self._set_controls_enabled(True)
        self.statusBar().showMessage(f"Merge complete — saved to {output_path}", 8000)
        QMessageBox.information(
            self,
            "Merge Complete",
            f"PDFs merged successfully.\n\nSaved to:\n{output_path}",
        )

    def _on_error(self, message: str) -> None:
        self._progress.setVisible(False)
        self._set_controls_enabled(True)
        self.statusBar().showMessage("Merge failed.", 5000)
        QMessageBox.critical(
            self,
            "Merge Failed",
            f"The merge could not be completed:\n\n{message}",
        )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for btn in (
            self._add_btn, self._remove_btn,
            self._up_btn, self._down_btn, self._merge_btn,
        ):
            btn.setEnabled(enabled)
        self._table.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {_APP_NAME}",
            f"<b>{_APP_NAME}</b><br>"
            f"Version {_APP_VER}<br><br>"
            "Merge PDF files with ease. Add files, choose which pages to include,<br>"
            "reorder as needed, and produce a single combined PDF.",
        )

    def _update_status(self) -> None:
        n = self._table.rowCount()
        if n == 0:
            self.statusBar().showMessage(
                "Ready — add PDF files using Add Files or drag & drop."
            )
        else:
            total_pages = sum(self._page_counts.values())
            self.statusBar().showMessage(
                f"{n} file{'s' if n != 1 else ''} · {total_pages} pages total  "
                "— double-click any row to select pages"
            )
