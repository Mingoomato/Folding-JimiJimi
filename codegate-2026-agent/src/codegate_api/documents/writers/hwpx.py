from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from codegate_api.documents.kordoc import KordocWorkerClient
from codegate_api.documents.models import (
    CreationPayload,
    DocumentFormat,
    DocumentOperation,
    HwpDerivationPayload,
    MarkdownCreationPayload,
    NativeLocator,
    StructuralDiff,
    StructureItem,
    TableCellSetOperation,
    TextReplaceOperation,
)
from codegate_api.documents.writers.base import ProposedDocument, WriterError


class HwpxWriter:
    format = DocumentFormat.HWPX
    fingerprint = "Kordoc/4.2.5"

    def __init__(self, *, worker: KordocWorkerClient, staging_root: Path) -> None:
        self._worker = worker
        self._staging_root = staging_root.resolve()

    def read_structure(self, source: bytes) -> list[StructureItem]:
        job = self._job()
        input_path = job / "input.hwpx"
        input_path.write_bytes(source)
        result = self._worker.request("parse", input=self._worker.relative(input_path))
        blocks = result.get("blocks", [])
        if not isinstance(blocks, list):
            raise WriterError("writer_protocol_error", "Kordoc blocks are malformed")
        return [
            StructureItem(
                locator=NativeLocator(kind="paragraph", block_index=index),
                value=block,
                value_type=(
                    str(block.get("type", "block")) if isinstance(block, dict) else "block"
                ),
            )
            for index, block in enumerate(blocks)
        ]

    def mutate(
        self,
        source: bytes,
        operations: list[DocumentOperation],
    ) -> ProposedDocument:
        if not all(
            isinstance(item, TextReplaceOperation | TableCellSetOperation) for item in operations
        ):
            raise WriterError("unsupported_operation", "HWPX operation is unsupported")
        job = self._job()
        input_path = job / "input.hwpx"
        output_path = job / "proposed.hwpx"
        edited_path = job / "edited.md"
        input_path.write_bytes(source)
        result = self._worker.request(
            "patchHwpx",
            input=self._worker.relative(input_path),
            output=self._worker.relative(output_path),
            editedMarkdown=self._worker.relative(edited_path),
            operations=[item.model_dump(mode="json") for item in operations],
        )
        skipped = result.get("skipped", [])
        if skipped:
            raise WriterError("partial_patch", "Kordoc skipped one or more operations")
        proposed = output_path.read_bytes()
        diffs = [
            StructuralDiff(
                operation_index=index,
                operation_type=operation.type,
                locator=operation.locator,
                before=operation.expected,
                after=operation.replacement,
            )
            for index, operation in enumerate(operations)
            if isinstance(operation, TextReplaceOperation | TableCellSetOperation)
        ]
        return ProposedDocument(proposed, diffs, [], self.fingerprint)

    def create(
        self,
        payload: CreationPayload,
        *,
        template: bytes | None = None,
    ) -> ProposedDocument:
        if template is not None:
            raise WriterError("unsupported_template", "HWPX templates are not supported")
        if not isinstance(payload, MarkdownCreationPayload):
            raise WriterError("unsupported_operation", "HWPX creation requires Markdown")
        job = self._job()
        output_path = job / "proposed.hwpx"
        self._worker.request(
            "markdownToHwpx",
            markdown=payload.markdown,
            output=self._worker.relative(output_path),
        )
        proposed = output_path.read_bytes()
        self._worker.request("parse", input=self._worker.relative(output_path))
        return ProposedDocument(proposed, [], [], self.fingerprint)

    def derive(self, source: bytes, payload: HwpDerivationPayload) -> ProposedDocument:
        job = self._job()
        input_path = job / "input.hwp"
        before_path = job / "template-before.hwpx"
        output_path = job / "proposed.hwpx"
        input_path.write_bytes(source)
        result = self._worker.request(
            "deriveHwpTemplate",
            input=self._worker.relative(input_path),
            beforeOutput=self._worker.relative(before_path),
            output=self._worker.relative(output_path),
            markdown=payload.markdown,
        )
        if payload.markdown is not None and not result.get("inserted"):
            raise WriterError(
                "unsupported_template",
                "HWP template has no supported daily-work two-column content area",
            )
        before = before_path.read_bytes()
        proposed = output_path.read_bytes()
        self._worker.request("parse", input=self._worker.relative(output_path))
        layout_value = result.get("layout")
        layout: dict[str, Any] = layout_value if isinstance(layout_value, dict) else {}
        compacted_lines = int(layout.get("compactedLines", 0))
        warnings = [
            "원본 HWP는 변경하지 않으며, 승인 시 편집 가능한 새 HWPX 파생본을 생성합니다.",
            (
                "HWP를 HWPX로 변환하면서 표 구조와 병합 셀은 유지하지만, "
                "시각 서식은 Kordoc이 재구성하므로 원본과 일부 다를 수 있습니다."
            ),
        ]
        if compacted_lines:
            warnings.append(
                f"고정 행 높이를 보존하기 위해 긴 표 셀 {compacted_lines}개를 "
                "말줄임표로 축약했습니다. 승인 미리보기에서 내용을 확인하세요."
            )
        return ProposedDocument(
            proposed,
            [
                StructuralDiff(
                    operation_index=0,
                    operation_type=payload.type,
                    before={"source_sha256": payload.expected_source_sha256},
                    after={
                        "format": "hwpx",
                        "conversion": "hwp_to_hwpx",
                        "template_mode": "converted_copy",
                        "original_preserved": True,
                        "template_filled": payload.markdown is not None,
                        "inserted_sections": result.get("inserted", []),
                        "layout": result.get("layout"),
                    },
                )
            ],
            warnings,
            self.fingerprint + "+hwp-template-profile-fixed-rows/v3",
            preview_before=before,
        )

    def render(self, source: bytes) -> list[tuple[str, bytes]]:
        job = self._job()
        input_path = job / "input.hwpx"
        output_dir = job / "render"
        output_dir.mkdir()
        input_path.write_bytes(source)
        result = self._worker.request(
            "render_document",
            input=self._worker.relative(input_path),
            outputDir=self._worker.relative(output_dir),
        )
        pages = result.get("pages", [])
        if not isinstance(pages, list):
            raise WriterError("writer_protocol_error", "Kordoc render output is malformed")
        rendered = []
        for index, relative in enumerate(pages[:20]):
            path = self._staging_root / str(relative)
            try:
                path.resolve().relative_to(self._staging_root)
            except ValueError as error:
                raise WriterError("unsafe_staging_path", "Kordoc render escaped staging") from error
            rendered.append((f"page-{index + 1}", path.read_bytes()))
        return rendered

    def _job(self) -> Path:
        path = self._staging_root / "kordoc-jobs" / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=False)
        return path


def canonical_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
