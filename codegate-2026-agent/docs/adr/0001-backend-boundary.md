# ADR 0001: Python 백엔드 분리와 저장 경계

- 상태: 채택
- 날짜: 2026-07-21

## 결정

Next.js 프론트와 FastAPI 백엔드를 두 저장소로 분리한다. FastAPI는 Claude Agent SDK for Python, `llm-wiki` adapter, 승인·버전·Undo, 실제 파일 쓰기의 단일 소유자다.

Claude Agent SDK는 built-in file/shell tool을 사용하지 않는다. `tools=[]`, `setting_sources=[]`, `permission_mode="dontAsk"`를 기본값으로 두고, backend가 정의한 read-only in-process MCP tool만 `allowed_tools`와 `PreToolUse` hook을 모두 통과시킨다. `CLAUDE_CONFIG_DIR`를 격리하고 auto memory와 prompt history를 비활성화한다. 실제 파일 적용은 agent tool이 아니라 application approval endpoint와 결정적 executor가 담당한다.

개발용 AI 지침은 `AGENTS.md`를 단일 원본으로 두고 Claude Code의 `CLAUDE.md`가 이를 import한다. 서비스 runtime은 이 파일들을 자동 로드하지 않는다. Versioned knowledge package의 `AGENT_GUIDE.md`는 checksum 목록 포함을 강제하고, 제한된 TOML frontmatter만 schema로 검증해 고정된 system prompt 문장으로 정규화한다. 자유 형식 Markdown 본문은 model context에 넣지 않는다.

검색의 source of truth는 용휘가 발행하는 versioned `llm-wiki`다. ACL은 검색 후보와 graph relation을 반환하기 전에 적용하며 권한 미확인 상태는 public read만 허용한다. MVP에서는 동일 문서를 Supabase pgvector에 다시 적재하지 않는다. 승인·실행 이력은 persistence port 뒤에 두고 로컬 개발은 SQLite, 호스팅·다중 사용자 단계는 Supabase Postgres adapter를 선택할 수 있게 한다.

웹은 Vercel, API는 Railway 배포를 목표로 한다. Railway에서 실제 파일 수정 데모를 제공하려면 source workspace와 SQLite를 영속 volume에 두어야 한다.

## 이유

- 핵심 경로가 Python 문서 처리, agent tool, 지식 그래프 adapter에 의존한다.
- 파일 수정은 LLM 호출과 분리된 결정적 executor와 사람 승인이 필요하다.
- 중복 vector store는 색인 동기화 실패와 24시간 구현 비용을 늘린다.
- 명시적 HTTP/OpenAPI 계약과 합성 fixture가 두 저장소 통합 위험을 줄인다.

## 검토했지만 선택하지 않은 대안

- 순수 Next.js 풀스택: 단순 LLM 호출에는 가장 빠르지만 현재 Python 중심 통합 경로를 이중 구현하게 된다.
- 단일 monorepo: 배포와 계약 공유에는 유리하지만 이미 역할별 저장소가 만들어졌고 파일 소유권이 분명하다. OpenAPI와 CI로 통합 경계를 관리한다.
- LangGraph + MCP 선도입: 현재 단일 agent와 typed function tool로 충분하다. 실제 다단계 상태 그래프나 독립 MCP provider가 생길 때 추가한다.
- Supabase pgvector 즉시 도입: `llm-wiki`의 vector/FTS와 책임이 겹친다. 검색팀 package가 부족하다는 측정 결과가 생기면 재검토한다.

## 후속 조건

- Claude Agent SDK model session을 붙이기 전 read-only 검색 수직 기능을 통과시킨다.
- 실제 쓰기 전 `plan_hash`, base hash, path allowlist, backup, atomic replace, Undo를 구현한다.
- 배포 전 Railway volume과 Supabase 사용 범위를 확정한다.
