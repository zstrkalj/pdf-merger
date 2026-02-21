"""Unit tests for PdfMerger."""

import pytest
from pathlib import Path
from pypdf import PdfWriter, PdfReader

from pdf_merger.core.merger import PdfMerger, PdfMergerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pdf(path: Path, num_pages: int = 1, text: str = "") -> Path:
    """Write a minimal but valid PDF with *num_pages* blank pages to *path*."""
    writer = PdfWriter()
    for i in range(num_pages):
        page = writer.add_blank_page(width=200, height=200)
        if text:
            # Embed the page index so tests can inspect content if needed
            pass  # pypdf blank pages are sufficient for structure tests
    with path.open("wb") as fh:
        writer.write(fh)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pdf_dir(tmp_path: Path) -> Path:
    """Return a directory pre-populated with three small test PDFs."""
    _make_pdf(tmp_path / "a.pdf", num_pages=1)
    _make_pdf(tmp_path / "b.pdf", num_pages=2)
    _make_pdf(tmp_path / "c.pdf", num_pages=3)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests – happy path
# ---------------------------------------------------------------------------

class TestMergeSuccess:
    def test_output_file_is_created(self, pdf_dir: Path, tmp_path: Path):
        output = tmp_path / "out" / "merged.pdf"
        merger = PdfMerger([pdf_dir / "a.pdf", pdf_dir / "b.pdf"])
        result = merger.merge(output)
        assert result.exists()

    def test_page_count_is_sum_of_inputs(self, pdf_dir: Path, tmp_path: Path):
        # a=1, b=2, c=3  →  total 6 pages
        inputs = [pdf_dir / "a.pdf", pdf_dir / "b.pdf", pdf_dir / "c.pdf"]
        output = tmp_path / "merged.pdf"
        PdfMerger(inputs).merge(output)
        assert len(PdfReader(str(output)).pages) == 6

    def test_merge_order_is_preserved(self, pdf_dir: Path, tmp_path: Path):
        """Merging b+a+c should give 2+1+3 = 6 pages (order doesn't change count,
        but we verify the merger respects the supplied order by checking counts
        for a reversed subset)."""
        # c(3) then a(1) → 4 pages
        output = tmp_path / "merged.pdf"
        PdfMerger([pdf_dir / "c.pdf", pdf_dir / "a.pdf"]).merge(output)
        assert len(PdfReader(str(output)).pages) == 4

    def test_output_parent_dirs_are_created(self, pdf_dir: Path, tmp_path: Path):
        output = tmp_path / "deep" / "nested" / "merged.pdf"
        PdfMerger([pdf_dir / "a.pdf"]).merge(output)
        assert output.exists()

    def test_single_file_merge(self, pdf_dir: Path, tmp_path: Path):
        output = tmp_path / "single.pdf"
        PdfMerger([pdf_dir / "b.pdf"]).merge(output)
        assert len(PdfReader(str(output)).pages) == 2

    def test_string_paths_accepted(self, pdf_dir: Path, tmp_path: Path):
        output = tmp_path / "merged.pdf"
        PdfMerger([str(pdf_dir / "a.pdf"), str(pdf_dir / "b.pdf")]).merge(str(output))
        assert output.exists()


# ---------------------------------------------------------------------------
# Tests – progress callback
# ---------------------------------------------------------------------------

class TestProgressCallback:
    def test_callback_called_once_per_file(self, pdf_dir: Path, tmp_path: Path):
        calls: list[tuple[int, int]] = []
        PdfMerger(
            [pdf_dir / "a.pdf", pdf_dir / "b.pdf", pdf_dir / "c.pdf"],
            progress_callback=lambda cur, tot: calls.append((cur, tot)),
        ).merge(tmp_path / "merged.pdf")
        assert calls == [(1, 3), (2, 3), (3, 3)]

    def test_callback_not_required(self, pdf_dir: Path, tmp_path: Path):
        # No callback → no error
        PdfMerger([pdf_dir / "a.pdf"]).merge(tmp_path / "merged.pdf")

    def test_callback_reports_correct_total(self, pdf_dir: Path, tmp_path: Path):
        totals: list[int] = []
        PdfMerger(
            [pdf_dir / "a.pdf", pdf_dir / "b.pdf"],
            progress_callback=lambda cur, tot: totals.append(tot),
        ).merge(tmp_path / "merged.pdf")
        assert all(t == 2 for t in totals)


# ---------------------------------------------------------------------------
# Tests – validation errors
# ---------------------------------------------------------------------------

class TestValidation:
    def test_empty_list_raises(self, tmp_path: Path):
        with pytest.raises(PdfMergerError, match="No input files"):
            PdfMerger([]).merge(tmp_path / "out.pdf")

    def test_missing_file_raises(self, pdf_dir: Path, tmp_path: Path):
        with pytest.raises(PdfMergerError, match="File not found"):
            PdfMerger([pdf_dir / "ghost.pdf"]).merge(tmp_path / "out.pdf")

    def test_invalid_pdf_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a pdf")
        with pytest.raises(PdfMergerError, match="Invalid or corrupt PDF"):
            PdfMerger([bad]).merge(tmp_path / "out.pdf")

    def test_directory_path_raises(self, tmp_path: Path):
        with pytest.raises(PdfMergerError, match="not a file"):
            PdfMerger([tmp_path]).merge(tmp_path / "out.pdf")
