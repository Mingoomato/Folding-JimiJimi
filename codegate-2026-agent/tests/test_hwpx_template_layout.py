from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LAYOUT_MODULE = (
    Path(__file__).parents[1] / "src/codegate_api/documents/workers/kordoc_template_layout.mjs"
)


def _run_layout(template: str, report: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    script = """
import { fillDailyWorkTemplate } from __MODULE__;
const input = JSON.parse(process.argv[1]);
const result = fillDailyWorkTemplate(input.template, input.report);
process.stdout.write(JSON.stringify(result));
""".replace("__MODULE__", json.dumps(LAYOUT_MODULE.resolve().as_uri()))
    completed = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            script,
            json.dumps({"template": template, "report": report}),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _daily_template(rows: int = 15) -> str:
    blank = '<tr><td colspan="2"></td><td colspan="5"></td></tr>'
    return "\n".join(
        [
            "<table>",
            '<tr><td colspan="2">금일 업무내용</td><td colspan="5">명일 업무내용</td></tr>',
            *([blank] * rows),
            '<tr><td colspan="7">비고</td></tr>',
            "</table>",
        ]
    )


def test_daily_template_wraps_long_items_into_fixed_existing_rows() -> None:
    result = _run_layout(
        _daily_template(),
        "\n".join(
            [
                "# 업무 보고서",
                "## 완료 업무",
                "- 구독 이탈률이 전주 대비 크게 상승하여 배송 지연 클레임과의 "
                "상관관계를 분석하고 물류팀 협의를 진행했습니다.",
                "## 다음 업무",
                "- 구독 이탈자 원인 인터뷰 일정을 조율하고 결과를 정산 보고서에 반영합니다.",
            ]
        ),
    )

    assert result["layout"] == {
        "rowCount": 15,
        "leftRowsUsed": 5,
        "rightRowsUsed": 3,
        "compactedLines": 0,
        "overflowPolicy": "compact_then_fail_closed",
    }
    markdown = str(result["markdown"])
    assert markdown.count("<tr>") == 17
    assert "상관관계를 분석하고" in markdown
    assert "물류팀 협의를" in markdown


def test_daily_template_rejects_more_items_than_fixed_rows_can_hold() -> None:
    report = "# 업무 보고서\n## 완료 업무\n" + "\n".join(
        f"- 매우 긴 업무 내용 {index} " + ("반복 내용 " * 20) for index in range(20)
    )
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    script = """
import { fillDailyWorkTemplate } from __MODULE__;
const input = JSON.parse(process.argv[1]);
try {
  fillDailyWorkTemplate(input.template, input.report);
  process.exit(2);
} catch (error) {
  process.stdout.write(String(error.message));
}
""".replace("__MODULE__", json.dumps(LAYOUT_MODULE.resolve().as_uri()))
    completed = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            script,
            json.dumps({"template": _daily_template(), "report": report}),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.stdout.startswith("template_content_overflow:")


def test_daily_template_recognizes_bold_section_headings_from_agent_response() -> None:
    result = _run_layout(
        _daily_template(),
        "\n".join(
            [
                "**업무 보고서**",
                "**완료 업무**",
                "* 오늘 완료한 업무입니다.",
                "**진행 업무**",
                "* 오늘 진행 중인 업무입니다.",
                "**주요 이슈**",
                "* 오늘 확인한 주요 이슈입니다.",
                "**다음 업무**",
                "* 내일 업무 1",
                "* 내일 업무 2",
            ]
        ),
    )

    markdown = str(result["markdown"])
    assert "완료 업무" in markdown
    assert "주요 이슈" in markdown
    assert "내일 업무 1" in markdown
    assert result["layout"]["leftRowsUsed"] == 7
    assert result["layout"]["rightRowsUsed"] == 2
