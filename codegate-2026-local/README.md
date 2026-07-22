# CODEGATE Local

문서 폴더를 로컬에서 변환·색인하고, 근거가 있는 검색과 승인 기반 수정을 제공하는 Electron
데스크톱 앱이다. React renderer와 Electron main process는 우창 저장소가 소유하고, main process가
성주의 Python sidecar, 용휘의 LLMWIKI, 민규의 doc2md를 loopback과 로컬 경로 계약으로 연결한다.

현재 production 지식 동기화는 폴더 하나씩 처리한다. 설정에서 기존 폴더를 해제한 뒤 다른 폴더를
등록할 수 있다. 로그인은 로컬 cloud-api의 Google OAuth + PKCE를 사용하며 callback은
`http://127.0.0.1:47821/auth/callback`으로 고정된다. 기본 답변 모델은 Gemini 2.5 Flash-Lite다.

## 개발 실행

요구 사항은 Node.js 18 이상, pnpm, uv, Python 3.12다. 팀 저장소 네 개를 같은 상위 디렉터리에
다음 이름으로 checkout한 구성을 기본으로 자동 탐색한다.

```text
Backend/             codegate-2026-agent
LLMWIKI/             codegate-2026-backend
codegate-2026-api/   codegate-2026-convert
codegate-2026-web/   codegate-2026-local
```

```bash
pnpm install
pnpm integrations:check
pnpm typecheck
pnpm test
pnpm dev:integrated
```

`dev:integrated`는 doc2md를 `uv run --isolated --python 3.12`로 먼저 띄우고 URL을 Electron에
전달한다. 이미 별도로 실행한 doc2md를 쓸 때만 `CODEGATE_DOC2MD_URL=... pnpm dev`를 사용한다.
네 저장소를 합친 산출물에서는 통합 루트 launcher가 루트 `.env`를 읽어 cloud-api와 이 앱을 함께
실행한다. 이 저장소만 직접 실행할 때는 [`.env.example`](.env.example)을 `.env`로 복사한다.

실제 네 저장소 프로세스를 함께 검증하려면 다음 smoke를 실행한다. Converter는 프로젝트 안에
`.venv`를 만들지 않고 `uv --isolated --python 3.12` 환경에서 실행한다.

```bash
pnpm integrations:smoke
```

smoke는 원본 fixture의 doc2md 변환, sidecar와 LLMWIKI 초기 build, 근거 검색, exact diff 승인,
새 version publish, Undo와 원본 복구를 순서대로 검증한다. 경로를 바꿔야 하면
[`.env.example`](.env.example)의 환경변수를 사용한다.

상세 실행·패키징 경계는 [통합 상태](docs/NEXT_INTEGRATION.md), 현재 구현은
[프론트엔드 스펙](docs/FRONTEND_SPEC.md), 실제 장애 기록은
[troubleshooting](docs/TROUBLESHOOTING.md)을 참고한다.

## Railway 웹 데모

Railway에는 Electron 프로세스가 아니라 renderer UI를 브라우저용으로 빌드한 웹 데모를 배포한다.
데스크톱 앱의 preload API는 웹 전용 메모리 어댑터로 대체되며, 지원 브라우저에서는 사용자가
선택한 폴더의 파일명만 브라우저 메모리에서 정리한다. 문서 내용과 API 키는 Railway로 전송하지
않고 실제 변환·Gemini 질의·원본 작업은 데스크톱 앱에서만 수행한다.

```bash
pnpm build:web
pnpm start:web
```

Railway는 [`Dockerfile.web`](Dockerfile.web), [`railway.toml`](railway.toml), `/health`를 사용한다.
상세 경계와 선택 근거는 [웹 배포 결정 기록](docs/WEB_DEPLOYMENT.md)에 정리했다.
