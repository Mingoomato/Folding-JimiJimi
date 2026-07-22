"""Convert a folder, writing chunks into processed/ or excepted/ plus a manifest.

The API only *judges* a document (``budget.routing``); placing files is this
module's job, because doc2md itself is a stateless HTTP service that does not own
an output directory — the backend does its own atomic writes against
``source://`` URIs and must not have a second writer behind its back.

**Folders, not filename prefixes.** A ``p_``/``n_`` prefix would change the
identifier: ``chunk_no`` is built from ``frontmatter.slugify(path.name)``, so
``test_1_pdf_001`` would become ``p_test_1_pdf_001`` and stop matching the stable
IDs the backend contract verifies — and a re-run would happily produce ``p_p_``.
Moving a file between directories leaves its name alone.

**manifest.jsonl alongside.** A directory records *that* something was excepted
but not *why*; the manifest carries the reason, the estimate, and the budget it
was measured against. The backend already reads a manifest.jsonl, so the shape is
familiar.

    python -m app.batch_cli <input-dir> <output-dir> [--budget-tokens N]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from app import budget as budget_mod
from app import cache, chunker, frontmatter, postprocess, structure
from app.converters import convert
from app.schemas import ChunkOptions, MetadataOverrides

SUPPORTED = {
    ".pdf", ".hwp", ".hwpx", ".pptx", ".ppt",
    ".docx", ".xlsx", ".csv", ".html", ".htm", ".md", ".txt",
}
MANIFEST = "manifest.jsonl"
ROUTES = (budget_mod.PROCESSED, budget_mod.EXCEPTED)


def _write_atomic(target: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A reader must never see a half-written chunk, and a crash mid-batch must not
    leave a truncated file that looks complete.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _clear_previous(out_root: Path, slug: str) -> None:
    """Drop this document's chunks from both routes before writing.

    Without this, lowering the budget between runs would leave the old copies in
    processed/ while the new ones land in excepted/, and the same document would
    exist twice with different classifications.
    """
    for route in ROUTES:
        folder = out_root / route
        if folder.is_dir():
            for stale in folder.glob(f"{slug}_*.md"):
                stale.unlink(missing_ok=True)


def convert_one(path: Path, out_root: Path, budget_tokens: int | None) -> dict:
    """Convert one file, place its chunks, and return its manifest row."""
    started = time.time()
    slug = frontmatter.slugify(path.name)

    raw = cache.get(path)
    if raw is None:
        raw = convert(path)
        cache.set(path, raw)
    cleaned = postprocess.clean(raw.body)

    fm = frontmatter.build(
        path, cleaned.body, MetadataOverrides(), title_hint=cleaned.title
    )
    est, budget_total, routing, reason = budget_mod.decide(cleaned.body, budget_tokens)
    diagnostics = list(raw.diagnostics) + structure.structural_diagnostics(
        cleaned.headings
    )

    chunks = chunker.build_document_chunks(
        path, fm, cleaned.body, ChunkOptions(enabled=True)
    )

    _clear_previous(out_root, slug)
    for chunk in chunks:
        _write_atomic(out_root / routing / f"{chunk.chunk_no}.md", chunk.markdown)

    return {
        "file": path.name,
        "sha256": fm.source.sha256,
        "canonical_sha256": frontmatter.canonical_sha256(cleaned.body),
        "routing": routing,
        "reason": reason,
        "est_tokens": est,
        "budget_tokens": budget_total,
        "chunks": len(chunks),
        "chars": len(cleaned.body),
        "sections": len(structure.extract_sections(cleaned.body)),
        "converter": raw.library_used,
        "format": raw.format,
        "diagnostics": [d.code for d in diagnostics],
        "seconds": round(time.time() - started, 2),
    }


def _is_document(p: Path) -> bool:
    """Office leaves lock files beside an open document — ``~$report.pptx`` — that
    carry the same extension but are a few hundred bytes of bookkeeping, and are
    usually unreadable while the app holds them. They are not input."""
    return (
        p.is_file()
        and p.suffix.lower() in SUPPORTED
        and not p.name.startswith("~$")
        and not p.name.startswith(".~lock.")
    )


def run(in_dir: Path, out_root: Path, budget_tokens: int | None = None) -> list[dict]:
    files = sorted(
        (p for p in in_dir.iterdir() if _is_document(p)),
        key=lambda p: p.name.lower(),
    )
    if not files:
        raise SystemExit(f"변환할 파일이 없습니다: {in_dir}")

    rows: list[dict] = []
    started = time.time()
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}  ({path.stat().st_size / 1e6:.1f}MB)")
        try:
            row = convert_one(path, out_root, budget_tokens)
        except Exception as e:
            row = {"file": path.name, "routing": "failed", "reason": str(e)}
            print(f"        FAILED: {e}")
        else:
            print(
                f"        {row['seconds']:>5.1f}s  {row['converter']:16} "
                f"{row['routing']:9} chunks={row['chunks']:<4} "
                f"sections={row['sections']:<4} ~{row['est_tokens']:,} tok"
            )
        rows.append(row)

    out_root.mkdir(parents=True, exist_ok=True)
    with (out_root / MANIFEST).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok = sum(1 for r in rows if r["routing"] == budget_mod.PROCESSED)
    exc = sum(1 for r in rows if r["routing"] == budget_mod.EXCEPTED)
    bad = sum(1 for r in rows if r["routing"] == "failed")
    print(
        f"\n=== {len(files)}개 | processed {ok} | excepted {exc} | failed {bad} "
        f"| {time.time() - started:.1f}초 ==="
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doc2md-batch", description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="Route documents over this estimate to excepted/ "
        "(default: $DOC2MD_TOKEN_BUDGET)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the output directory before starting",
    )
    args = parser.parse_args(argv)

    if args.clean and args.output_dir.exists():
        shutil.rmtree(args.output_dir)

    run(args.input_dir, args.output_dir, args.budget_tokens)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8")
    raise SystemExit(main())
