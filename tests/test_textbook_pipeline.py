"""Tests for textbook pipeline integration and local cache confinement."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.translate_all_chapters import full_textbook_pipeline
from scripts.translate_book import translate_book_pipeline


class TextbookPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.output_dir = Path(self.tmpdir) / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.fake_pdf = Path(self.tmpdir) / "sample.pdf"
        self.fake_pdf.write_bytes(b"%PDF-1.7\nfake pdf content\n%%EOF")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pipeline_exports_and_callable(self) -> None:
        self.assertTrue(callable(full_textbook_pipeline))
        self.assertTrue(callable(translate_book_pipeline))

    @patch("scripts.translate_all_chapters.extract_chapters_from_pdf")
    @patch("scripts.translate_all_chapters.translate_book_pipeline")
    def test_single_chapter_falls_back_to_book_pipeline(
        self, mock_book_pipeline, mock_extract
    ) -> None:
        mock_extract.return_value = []
        from scripts.translate_pdf import Translation
        mock_book_pipeline.return_value = Translation(self.output_dir / "sample-vi.pdf", 0)

        res = full_textbook_pipeline(
            self.fake_pdf,
            self.output_dir,
            target_language="vi",
        )
        self.assertEqual(res.path, self.output_dir / "sample-vi.pdf")
        mock_book_pipeline.assert_called_once()
        self.assertEqual(os.environ.get("PDF2ZH_CACHE_DIR"), str(self.output_dir))

    @patch("scripts.translate_all_chapters.extract_chapters_from_pdf")
    @patch("scripts.translate_all_chapters.translate_book_pipeline")
    @patch("scripts.translate_all_chapters.merge_pdfs")
    def test_multi_chapter_translates_and_merges(
        self, mock_merge, mock_book_pipeline, mock_extract
    ) -> None:
        ch1 = self.output_dir / "chapters" / "01_Intro.pdf"
        ch2 = self.output_dir / "chapters" / "02_Anatomy.pdf"
        ch1.parent.mkdir(parents=True, exist_ok=True)
        ch1.touch()
        ch2.touch()

        mock_extract.return_value = [
            {"path": str(ch1), "title": "Intro"},
            {"path": str(ch2), "title": "Anatomy"},
        ]

        from scripts.translate_pdf import Translation
        def fake_translate(pdf_path, out_dir, **kwargs):
            out_pdf = out_dir / f"{pdf_path.stem}-vi.pdf"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_pdf.touch()
            return Translation(out_pdf, 0)

        mock_book_pipeline.side_effect = fake_translate

        def fake_merge(pdfs, final):
            final.touch()
            return 0

        mock_merge.side_effect = fake_merge

        progress_events = []
        def on_prog(stage, d, t):
            progress_events.append((stage, d, t))

        res = full_textbook_pipeline(
            self.fake_pdf,
            self.output_dir,
            target_language="vi",
            on_progress=on_prog,
        )

        expected_final = self.output_dir / "sample-vi.pdf"
        self.assertEqual(res.path, expected_final)
        self.assertEqual(mock_book_pipeline.call_count, 2)
        mock_merge.assert_called_once()
        self.assertTrue(len(progress_events) > 0)

    @patch("scripts.translate_all_chapters.extract_chapters_from_pdf")
    @patch("scripts.translate_all_chapters.translate_book_pipeline")
    @patch("scripts.translate_all_chapters.merge_pdfs")
    def test_skips_already_translated_chapters(
        self, mock_merge, mock_book_pipeline, mock_extract
    ) -> None:
        ch1 = self.output_dir / "chapters" / "01_Intro.pdf"
        ch2 = self.output_dir / "chapters" / "02_Anatomy.pdf"
        ch1.parent.mkdir(parents=True, exist_ok=True)
        ch1.touch()
        ch2.touch()

        # Pre-create chapter 1 output so it's already translated
        out1 = self.output_dir / "chapter_outputs" / "01_Intro" / "01_Intro-vi.pdf"
        out1.parent.mkdir(parents=True, exist_ok=True)
        out1.touch()

        mock_extract.return_value = [
            {"path": str(ch1), "title": "Intro"},
            {"path": str(ch2), "title": "Anatomy"},
        ]

        from scripts.translate_pdf import Translation
        def fake_translate(pdf_path, out_dir, **kwargs):
            out_pdf = out_dir / f"{pdf_path.stem}-vi.pdf"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_pdf.touch()
            return Translation(out_pdf, 0)

        mock_book_pipeline.side_effect = fake_translate

        def fake_merge(pdfs, final):
            self.assertEqual(len(pdfs), 2)
            self.assertEqual(pdfs[0], out1)
            final.touch()
            return 0

        mock_merge.side_effect = fake_merge

        res = full_textbook_pipeline(
            self.fake_pdf,
            self.output_dir,
            target_language="vi",
        )

        expected_final = self.output_dir / "sample-vi.pdf"
        self.assertEqual(res.path, expected_final)
        # Only chapter 2 should be translated since chapter 1 already exists
        self.assertEqual(mock_book_pipeline.call_count, 1)
        mock_merge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
