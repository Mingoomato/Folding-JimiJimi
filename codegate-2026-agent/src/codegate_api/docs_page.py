from __future__ import annotations

from html import escape
from typing import Any

from fastapi.responses import HTMLResponse

_METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "patch": 3, "delete": 4}


def render_api_docs(*, app_name: str, version: str, schema: dict[str, Any]) -> HTMLResponse:
    operations: list[tuple[str, str, str, str]] = []
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in _METHOD_ORDER or not isinstance(operation, dict):
                continue
            operations.append(
                (
                    method,
                    path,
                    str(operation.get("summary") or "설명 없음"),
                    str(operation.get("description") or ""),
                )
            )
    operations.sort(key=lambda item: (item[1], _METHOD_ORDER[item[0]]))

    rows = "".join(
        f"""
        <article class="endpoint">
          <span class="method {escape(method)}">{escape(method.upper())}</span>
          <code>{escape(path)}</code>
          <strong>{escape(summary)}</strong>
          {f"<p>{escape(description)}</p>" if description else ""}
        </article>
        """
        for method, path, summary, description in operations
    )
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(app_name)} API</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, Pretendard, -apple-system, BlinkMacSystemFont,
      "Segoe UI", sans-serif; color: #172033; background: #f5f7fb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    main {{ width: min(960px, calc(100% - 32px)); margin: 48px auto 80px; }}
    header {{ margin-bottom: 28px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(28px, 5vw, 44px); letter-spacing: -0.04em; }}
    header p {{ margin: 8px 0; color: #58647a; line-height: 1.65; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 32px; }}
    nav a {{ color: #2b59c3; background: #e9efff; padding: 10px 14px; border-radius: 10px;
      text-decoration: none; font-weight: 700; }}
    section {{ display: grid; gap: 12px; }}
    .endpoint {{ display: grid; grid-template-columns: 70px minmax(220px, 1fr) 1.2fr;
      gap: 14px; align-items: center; padding: 17px 18px; background: white;
      border: 1px solid #e3e8f2;
      border-radius: 14px; box-shadow: 0 5px 18px rgba(23, 32, 51, .04); }}
    .endpoint p {{ grid-column: 2 / -1; margin: -3px 0 0; color: #68748a; line-height: 1.55; }}
    .method {{ width: 62px; padding: 7px 0; border-radius: 8px; text-align: center;
      color: white; font-size: 12px; font-weight: 800; letter-spacing: .04em; }}
    .get {{ background: #237a57; }} .post {{ background: #2b59c3; }}
    .put, .patch {{ background: #a15c00; }} .delete {{ background: #b42336; }}
    code {{ overflow-wrap: anywhere; color: #172033; font-size: 14px; }}
    strong {{ font-size: 14px; }}
    @media (max-width: 720px) {{
      main {{ margin-top: 28px; }}
      .endpoint {{ grid-template-columns: 64px 1fr; }}
      .endpoint strong, .endpoint p {{ grid-column: 1 / -1; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{escape(app_name)}</h1>
      <p>버전 {escape(version)} · Backend와 문서 Converter가 연결된 통합 API입니다.</p>
      <p>보호된 API는 <code>Authorization: Bearer &lt;token&gt;</code>을 사용합니다.</p>
      <nav>
        <a href="/api/v1/health">서버 상태 보기</a>
        <a href="/openapi.json">OpenAPI JSON</a>
      </nav>
    </header>
    <section aria-label="API endpoints">{rows}</section>
  </main>
</body>
</html>"""
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
