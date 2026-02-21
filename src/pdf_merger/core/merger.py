"""PDF merging logic using pypdf."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


class PdfMergerError(Exception):
    """Raised when a merge operation cannot be completed."""


class PdfMerger:
    """Merge an ordered list of PDF files into a single output file.

    Args:
        paths: Ordered list of paths to the PDF files to merge.
        progress_callback: Optional callable that receives ``(current, total)``
            after each file is appended.  ``current`` is 1-based.
        pages_per_file: Optional per-file page selection.  Each element maps
            1-to-1 with *paths*: a ``list[int]`` of **0-based** page indices
            to include, or ``None`` to include all pages of that file.
            When the outer list itself is ``None`` every file's pages are
            included in full (default behaviour).
    """

    def __init__(
        self,
        paths: list[str | Path],
        progress_callback: Callable[[int, int], None] | None = None,
        pages_per_file: list[list[int] | None] | None = None,
    ) -> None:
        self._paths = [Path(p) for p in paths]
        self._progress_callback = progress_callback
        # Normalise: pad with None so indexing is always safe
        if pages_per_file is None:
            self._pages_per_file: list[list[int] | None] = [None] * len(self._paths)
        else:
            self._pages_per_file = list(pages_per_file)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def merge(self, output_path: str | Path) -> Path:
        """Validate, merge, and save the PDFs.

        Args:
            output_path: Destination path for the merged PDF.

        Returns:
            The resolved output path.

        Raises:
            PdfMergerError: If no input files are given, a file is missing,
                or a file is not a valid PDF.
        """
        if not self._paths:
            raise PdfMergerError("No input files provided.")

        self._validate_all()

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        writer = PdfWriter()
        total = len(self._paths)

        for index, path in enumerate(self._paths, start=1):
            reader = PdfReader(str(path))
            page_indices = self._pages_per_file[index - 1]
            if page_indices is None:
                for page in reader.pages:
                    writer.add_page(page)
            else:
                for idx in page_indices:
                    writer.add_page(reader.pages[idx])

            if self._progress_callback is not None:
                self._progress_callback(index, total)

        with output_path.open("wb") as fh:
            writer.write(fh)

        return output_path.resolve()

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_all(self) -> None:
        for path in self._paths:
            self._validate_one(path)

    @staticmethod
    def _validate_one(path: Path) -> None:
        if not path.exists():
            raise PdfMergerError(f"File not found: {path}")
        if not path.is_file():
            raise PdfMergerError(f"Path is not a file: {path}")
        try:
            PdfReader(str(path))
        except PdfReadError as exc:
            raise PdfMergerError(f"Invalid or corrupt PDF '{path}': {exc}") from exc
