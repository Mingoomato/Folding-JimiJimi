"""doc2md의 /convert 엔드포인트를 Claude Agent SDK 툴로 노출하는 예제.

사전 준비:
    uv sync --extra agent
    (별도 터미널) uv run uvicorn app.main:app --port 8000

실행:
    uv run python agent_tool_example.py "C:/path/to/문서.hwpx"
"""

import asyncio
import sys

import httpx
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

DOC2MD_URL = "http://localhost:8000/convert"


@tool(
    "convert_to_markdown",
    "그래프에서 찾은 문서 파일(pdf/docx/xlsx/pptx/hwp/hwpx 등)을 "
    "YAML 프론트매터가 포함된 마크다운으로 변환한다",
    {"path": str},
)
async def convert_to_markdown(args: dict) -> dict:
    path = args["path"]
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(DOC2MD_URL, json={"path": path})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return {
                "content": [{"type": "text", "text": f"변환 실패: {e}"}],
                "is_error": True,
            }
    markdown = resp.json()["markdown"]
    return {"content": [{"type": "text", "text": markdown}]}


doc2md_server = create_sdk_mcp_server(
    name="doc2md",
    version="0.1.0",
    tools=[convert_to_markdown],
)


async def main(target_path: str) -> None:
    options = ClaudeAgentOptions(
        mcp_servers={"doc2md": doc2md_server},
        allowed_tools=["mcp__doc2md__convert_to_markdown"],
        # 이 SDK가 내부적으로 띄우는 CLI 서브프로세스에도 UTF-8을 강제 —
        # Windows 콘솔 기본 코드페이지(cp949)로 인한 한글 깨짐 방지.
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"이 파일을 마크다운으로 변환해줘: {target_path}")
        async for message in client.receive_response():
            print(message)


if __name__ == "__main__":
    # 이 스크립트 자체의 stdout/stderr도 UTF-8로 강제 (app/main.py와 동일한 이유).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if len(sys.argv) != 2:
        print("usage: python agent_tool_example.py <path>")
        raise SystemExit(1)

    asyncio.run(main(sys.argv[1]))
