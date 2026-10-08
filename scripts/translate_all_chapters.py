#!/usr/bin/env python3
"""Batch translate all chapters and merge into the final Vietnamese textbook."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.merge_pdfs import merge_pdfs
from scripts.split_pdf_by_chapters import extract_chapters_from_pdf
from scripts.translate_book import translate_book_pipeline
from scripts.translate_pdf import Translation, TranslationError
from pdf2zh.profiles import list_available_profiles, resolve_profile_dir


def full_textbook_pipeline(
    input_pdf: Path,
    output_dir: Path,
    *,
    target_language: str = "vi",
    source_language: str = "auto",
    profile: str | None = "dental",
    system_prompt: Path | None = None,
    glossary: Path | None = None,
    extra_envs: dict[str, str] | None = None,
    concurrency: int = 2,
    request_interval: float = 0.2,
    threads: int = 4,
    overwrite: bool = False,
    force_retranslate: bool = False,
    on_progress: object = None,
) -> Translation:
    """Full textbook workflow: Split -> Batch Translate (Checkpoint) -> Merge TOC.

    All intermediate chapters, checkpoints, and caches stay strictly inside output_dir.
    """
    input_pdf = input_pdf.expanduser().resolve()
    if not input_pdf.is_file():
        raise TranslationError(f"Input PDF does not exist: {input_pdf}")

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Strictly bind all cache to output_dir
    os.environ["PDF2ZH_CACHE_DIR"] = str(output_dir)
    try:
        from pdf2zh.cache import init_db
        init_db(output_dir)
    except Exception:
        pass

    final_pdf = output_dir / f"{input_pdf.stem}-{target_language}.pdf"
    if final_pdf.exists() and not overwrite and not force_retranslate:
        return Translation(final_pdf, 0)

    # 1. Split into chapters
    if callable(on_progress):
        on_progress("Phân tích mục lục & Tách chương", 0, 100)

    chapters_dir = output_dir / "chapters"
    chapters = extract_chapters_from_pdf(input_pdf, chapters_dir)

    # If no chapters found or only 1 chapter, run translate_book_pipeline directly
    if len(chapters) <= 1:
        return translate_book_pipeline(
            input_pdf,
            output_dir,
            target_language=target_language,
            source_language=source_language,
            threads=threads,
            concurrency=concurrency,
            request_interval=request_interval,
            profile=profile,
            system_prompt=system_prompt,
            glossary=glossary,
            extra_envs=extra_envs,
            overwrite=overwrite,
            force_retranslate=force_retranslate,
            on_progress=on_progress,
        )

    # 2. Batch translate chapters
    translated_pdfs: list[Path] = []
    total_chapters = len(chapters)
    total_untranslated = 0

    base_chapters_out = output_dir / "chapter_outputs"
    base_chapters_out.mkdir(parents=True, exist_ok=True)

    for idx, ch_info in enumerate(chapters, start=1):
        ch_path = Path(ch_info["path"])
        ch_name = ch_path.stem
        ch_out = base_chapters_out / ch_name
        ch_out.mkdir(parents=True, exist_ok=True)

        # Check if chapter already completed
        out_candidates = sorted(ch_out.glob(f"*-{target_language}.pdf")) or sorted(ch_out.glob("*-vi.pdf"))
        if out_candidates and not force_retranslate:
            if callable(on_progress):
                on_progress(f"Chương {idx}/{total_chapters}: Đã hoàn tất", idx, total_chapters)
            translated_pdfs.append(out_candidates[0])
            continue

        def _chapter_progress(stage: str, done: int, total: int, ch_num=idx) -> None:
            if callable(on_progress):
                on_progress(f"Chương {ch_num}/{total_chapters} ({stage})", done, total)

        res = translate_book_pipeline(
            ch_path,
            ch_out,
            target_language=target_language,
            source_language=source_language,
            threads=threads,
            concurrency=concurrency,
            request_interval=request_interval,
            profile=profile,
            system_prompt=system_prompt,
            glossary=glossary,
            extra_envs=extra_envs,
            overwrite=overwrite,
            force_retranslate=force_retranslate,
            on_progress=_chapter_progress,
        )

        if res.path and res.path.is_file():
            translated_pdfs.append(res.path)
            total_untranslated += res.untranslated
        else:
            raise TranslationError(f"Failed to translate chapter {idx} ({ch_path.name})")

    # 3. Merge TOC into final PDF
    if callable(on_progress):
        on_progress("Gộp các chương & Mục lục TOC", 0, 1)

    merge_res = merge_pdfs(translated_pdfs, final_pdf)
    if merge_res != 0 or not final_pdf.is_file():
        raise TranslationError(f"Failed to merge {len(translated_pdfs)} chapters into final textbook: {final_pdf}")

    return Translation(final_pdf, total_untranslated)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Batch translate all textbook chapters and merge final PDF."
    )
    parser.add_argument(
        "--chapters-dir",
        type=Path,
        default=SKILL_ROOT / "books" / "chapters",
        help="Directory containing split chapter PDFs (default: books/chapters)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SKILL_ROOT / "output" / "chapters",
        help="Base output directory for chapters (default: output/chapters)",
    )
    parser.add_argument(
        "--final-pdf",
        type=Path,
        default=SKILL_ROOT / "output" / "translated_book.pdf",
        help="Final merged PDF path (default: output/translated_book.pdf)",
    )
    available = ", ".join(list_available_profiles(SKILL_ROOT)) or "dental, general_medicine"
    parser.add_argument(
        "--profile",
        type=str,
        default="dental",
        help=f"Medical profile name (available: {available}) or path (default: dental)",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        default=None,
        help="Path to system prompt file (defaults to profile prompt)",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=None,
        help="Path to glossary file (defaults to profile glossary)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Translation concurrency (default: 2)",
    )
    parser.add_argument(
        "--start-chapter",
        type=int,
        default=1,
        help="Chapter index to start from (1-based, default: 1)",
    )
    parser.add_argument(
        "--end-chapter",
        type=int,
        default=999,
        help="Chapter index to end at (inclusive)",
    )
    parser.add_argument(
        "--skip-merge",
        action="store_true",
        help="Only translate chapters, do not merge final PDF",
    )
    parser.add_argument(
        "--force-retranslate",
        action="store_true",
        help="Re-translate all chapters, ignoring existing completed PDFs",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    chapters_dir = args.chapters_dir.resolve()
    base_output = args.output_dir.resolve()
    base_output.mkdir(parents=True, exist_ok=True)

    if not chapters_dir.is_dir():
        print(f"Error: Chapters directory not found: {chapters_dir}", file=sys.stderr)
        return 1

    chapter_files = sorted([f for f in chapters_dir.glob("*.pdf")])
    if not chapter_files:
        print(f"Error: No PDF files found in {chapters_dir}", file=sys.stderr)
        return 1

    total_chapters = len(chapter_files)
    print("=" * 65)
    print(f"BATCH TEXTBOOK TRANSLATION: {total_chapters} chapters discovered")
    print(f"Profile:          {args.profile}")
    print(f"Output Directory: {base_output}")
    if args.system_prompt:
        print(f"System Prompt:    {args.system_prompt}")
    if args.glossary:
        print(f"Glossary:         {args.glossary}")
    print("=" * 65)

    translated_pdfs = []
    start_total_time = time.time()

    for idx, chapter_pdf in enumerate(chapter_files, start=1):
        if idx < args.start_chapter or idx > args.end_chapter:
            continue

        ch_name = chapter_pdf.stem
        ch_output = base_output / ch_name
        ch_output.mkdir(parents=True, exist_ok=True)

        # Check if already completed
        out_candidates = list(ch_output.glob("*-vi.pdf"))
        if out_candidates and not args.force_retranslate:
            print(f"\n✓ [{idx}/{total_chapters}] Chapter already completed: {out_candidates[0].name}")
            translated_pdfs.append(out_candidates[0])
            continue

        print(f"\n>>> [{idx}/{total_chapters}] Translating: {chapter_pdf.name}")
        ch_start = time.time()

        cmd = [
            sys.executable,
            str(SKILL_ROOT / "scripts" / "translate_book.py"),
            str(chapter_pdf),
            "--output-dir",
            str(ch_output),
            "--profile",
            str(args.profile),
            "--concurrency",
            str(args.concurrency),
            "--overwrite",
        ]
        if args.force_retranslate:
            cmd.append("--force-retranslate")
        if args.system_prompt:
            cmd.extend(["--system-prompt", str(args.system_prompt)])
        if args.glossary:
            cmd.extend(["--glossary", str(args.glossary)])

        result = subprocess.run(cmd)
        ch_elapsed = time.time() - ch_start

        if result.returncode != 0:
            print(
                f"ERROR: Failed translating chapter {idx} ({chapter_pdf.name}) with code {result.returncode}",
                file=sys.stderr,
            )
            return result.returncode

        # Locate translated output PDF
        out_candidates = list(ch_output.glob("*-vi.pdf"))
        if out_candidates:
            translated_pdfs.append(out_candidates[0])
            print(f"✓ [{idx}/{total_chapters}] Done in {ch_elapsed:.0f}s -> {out_candidates[0].name}")
        else:
            print(f"Warning: No -vi.pdf found in {ch_output}", file=sys.stderr)

    total_elapsed = time.time() - start_total_time
    print("\n" + "=" * 65)
    print(f"ALL CHAPTERS TRANSLATED in {total_elapsed / 60:.1f} minutes")
    print("=" * 65)

    if not args.skip_merge and translated_pdfs:
        print("\n>>> Merging all translated chapters into final textbook...")
        merge_res = merge_pdfs(translated_pdfs, args.final_pdf.resolve())
        if merge_res == 0:
            print(f"\n★ FINAL BOOK READY: {args.final_pdf.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
