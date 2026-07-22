# 통합 상태와 실행 기준

## 2026-07-22 검증 기준

GitHub의 네 CODEGATE 저장소 기본 브랜치와 전체 PR을 확인했다. 열린 PR은 없고 확인된 PR은 모두
병합 상태다. 이 브랜치가 검증한 기준은 다음과 같다.

| 담당 | 저장소 | 검증 commit |
|---|---|---|
| 성주 | `dotenv-uploaded/codegate-2026-agent` | `b4563ea` (PR #11) |
| 용휘 | `dotenv-uploaded/codegate-2026-backend` | `acf94f7` |
| 민규 | `dotenv-uploaded/codegate-2026-convert` | `beb1ca8` |
| 우창 | `dotenv-uploaded/codegate-2026-local` | base `94ba7e1` + `integration/local-sidecar-runtime` |

`pnpm integrations:smoke`는 프로젝트 안에 Python venv를 만들거나 커밋하지 않는다. 기본값으로
`uv run --isolated --python 3.12`를 사용하며, 이미 관리 중인 interpreter가 있으면
`CODEGATE_DOC2MD_PYTHON`으로 명시할 수 있다.

## 통합 산출물에서 고정한 경계

- 인증은 Supabase가 아니라 `codegate-2026-backend/cloud-api`의 Google OAuth + PKCE를 사용한다.
  로컬 개발 기본 URL은 `http://127.0.0.1:8000/api/v1`, callback은
  `http://127.0.0.1:47821/auth/callback`이다.
- 기본 agent mode는 `gemini`, 모델은 `gemini-2.5-flash-lite`다. Gemini 모드에는 Anthropic 키가
  필요하지 않으며 Gemini 키·모델·timeout·data policy만 sidecar에 전달한다.
- Gemini locate 응답은 현재 turn의 검증된 문서만 요약하고, 원문 표와 citation은
  본문에 중복하지 않는다. 출처는 Electron citation chip으로 별도 표시한다.
- Electron은 문서 폴더가 선택된 뒤에만 `codegate-local`을 시작한다.
- production 동기화는 폴더 하나씩만 지원한다. UI와 main service가 모두 두 번째 폴더를 막고,
  기존 폴더를 해제한 뒤 새 폴더를 등록한다.
- 폴더 등록은 원본 SHA를 고정해 doc2md로 변환하고 LLMWIKI 1.0 입력을 만든 뒤 sidecar를 시작한다.
- 같은 source URI와 SHA의 입력은 덮어쓰지 않고, 변경된 원본만 revision을 올린다.
- rename은 app-owned manifest로 기존 document ID를 유지하고, 예약문자는 source URI segment별로 encoding한다.
- source/input/storage/data는 realpath 기준 비중첩이며 ownership marker 없는 기존 input 폴더에는 쓰지 않는다.
- sidecar에는 source, LLMWIKI bundle, normalized input, native storage, app data의 서로 겹치지 않는 경로를 전달한다.
- 개발 환경은 sibling `Backend/.venv/bin/codegate-local`과 `LLMWIKI` checkout을 자동 탐색한다.
- 설치본 runtime 경로 계약은 `resources/sidecar/codegate-local`과 `resources/LLMWIKI`다. 현재 저장소에는
  플랫폼별 standalone sidecar artifact가 없으므로 설치본 publish command를 제공하지 않는다.

## 민규 산출물 연결

1. `pnpm dev:integrated`로 doc2md v0.2와 Electron을 함께 실행한다.
2. 별도 실행 시 인증정보 없는 loopback HTTP URL만 `CODEGATE_DOC2MD_URL`에 설정한다.
3. Electron 변환 package가 준비되면 `CODEGATE_CONVERT_MODULE`을 설정한다.
4. kordoc 실행 파일이 준비되면 `CODEGATE_KORDOC_BIN`을 설정한다.
5. `pnpm integrations:check -- --strict`로 `toInputContract`, deep health, binary를 확인한다.

Backend가 소비하는 doc2md 계약은 `POST /v2/convert`, `GET /capabilities`, `GET /health?deep=1`이다.
별도 process에서는 bytes transport와 Bearer secret을 사용하고, 같은 filesystem에서는 path transport를 사용할 수 있다.

## 용휘 산출물 연결

1. 검증한 LLMWIKI checkout 또는 bundle 경로를 `CODEGATE_LLMWIKI_PROJECT_ROOT`에 설정한다.
2. Electron builder package가 준비되면 `CODEGATE_BUILDER_MODULE`을 설정한다.
3. native storage는 `tenants/<tenant>/wikis/<wiki>/current.json`을 유일한 활성 포인터로 사용한다.
4. `pnpm integrations:check -- --strict`로 bundle과 `assemble` export를 확인한다.

## 통합 전 실행

```bash
pnpm integrations:check
pnpm integrations:smoke
pnpm typecheck
pnpm test
pnpm build
```

`integrations:check`는 sibling checkout과 선택적 외부 package 상태를 빠르게 확인한다.
`integrations:smoke`는 현재 팀 산출물의 실제 Python 프로세스와 HTTP 계약을 검증하는 acceptance
gate다. 외부 npm package와 kordoc binary까지 제공되는 배포 구성을 검증할 때는
`integrations:check --strict`도 통과해야 한다.

## 설치본 E2E

```text
폴더 선택
→ sidecar ready
→ 최초 변환·native build
→ 근거가 있는 질문 응답
→ exact diff와 plan_hash 승인
→ 원본 변경·재빌드
→ 새 근거 검색
→ Undo
→ 앱 종료 뒤 sidecar process 없음
```

Railway는 Electron 또는 로컬 sidecar 배포 대상이 아니다. 향후 별도 cloud API가 필요할 때만 Backend의 `railway.toml`을 사용한다.

현재 smoke는 정규화 Markdown fixture를 선복사하지 않고 원본을 doc2md로 변환해 최초 native build를
만든다. 플랫폼별 standalone sidecar/doc2md와 서명된 Electron 설치본을 만드는 release pipeline은
아직 없으므로, source 통합과 별개인 최종 패키징 gate로 남아 있다. 이 artifact 없이 installer를
만들어 동작하는 것처럼 배포하지 않는다.

## 2026-07-22 전달 상태

- 원본 등록 직후 메인 화면 전환, 백그라운드 변환 진행률, 폴더 트리 파일 표시, sidecar crash 정리,
  watcher 재동기화와 sidecar 연결을 `integration/local-sidecar-runtime`에 통합했다.
- `pnpm test` 55개 통과(실제 통합 1개 opt-in skip), `pnpm build`, `pnpm integrations:smoke` 통과.
- 실제 사용자 질문에서 Backend PR #11과 함께 단일 신청서의 짧은 요약과 출처 한 건을 확인했다.
