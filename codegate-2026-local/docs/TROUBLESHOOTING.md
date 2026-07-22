# Troubleshooting

## 2026-07-22 — Electron renderer를 Railway에 올리면 첫 로딩이 끝나지 않음

### 상황과 영향

Railway에서 프런트 화면을 공개하려 했지만 저장소의 프런트는 일반 웹 앱이 아니라 Electron
renderer였다. renderer 산출물만 정적 서빙하면 화면은 내려오지만 앱 초기화가 완료되지 않아
사용자가 제품 UI에 진입할 수 없다.

### 기대와 실제

- 기대: Railway URL에서 로그인과 문서 화면을 확인한다.
- 실제: `useSession`, `useWorkspace`, `useApproval`이 시작과 동시에 `window.codegate`를 호출하지만
  브라우저에는 Electron preload가 없어 첫 로딩에서 실패한다.

### 근거와 원인

`src/preload/index.ts`가 `contextBridge.exposeInMainWorld`로 API를 주입하고 main process가 OS 폴더,
SQLite, child process sidecar를 소유한다. Railway 컨테이너는 이 preload와 사용자 로컬
파일시스템을 브라우저에 제공할 수 없다. 배포 설정 문제가 아니라 런타임 경계 불일치가 원인이다.

### 검토한 대안

- Electron을 headless container에서 실행하는 방식은 사용자에게 창과 로컬 파일 접근을 제공하지
  못해 기각했다.
- renderer 정적 파일만 서빙하는 방식은 preload 계약이 없어 기각했다.
- 실제 API 키를 브라우저 번들에 넣는 방식은 비밀 노출 때문에 기각했다.

### 해결

공용 `CodegateApi` 타입을 Electron 구현에서 분리하고 웹 전용 메모리 bridge를 추가했다. 웹은
폴더 파일명·트리·채팅 UI만 데모하며 실제 문서 분석과 수정은 하지 않는다. `vite.web.config.ts`,
비밀 제외 `.dockerignore`, multi-stage `Dockerfile.web`, 보안 헤더를 주는 Node 정적 서버와 Railway
healthcheck를 추가했다.

### 검증과 회귀 방지

웹 타입검사와 빌드, 기존 Electron 타입검사·55개 테스트·production build, Railway용 Docker build와
컨테이너 `/health`, SPA fallback, CSP를 모두 확인했다. 앞으로 Electron API가 바뀌면 두 bridge가
같은 `CodegateApi` 인터페이스를 구현해야 타입검사를 통과한다.

### 남은 위험과 인터뷰 질문

웹 데모는 새로고침 시 상태가 사라지고 실제 문서 내용을 분석하지 않는다. 실제 웹 제품 전환에는
tenant 격리, 업로드 수명주기, 변환 worker, 서버 비밀 관리가 필요하다. 설명할 핵심 질문은
“데스크톱 전용 기능을 웹 데모로 분리하면서 보안 경계를 어떻게 보존했는가?”다.

## 2026-07-22 — 실제 사용자 문서의 중복 H2가 sidecar 시작을 막음

### 맥락과 영향

doc2md 변환과 normalized input 저장은 끝났지만 sidecar startup의 LLMWIKI compatibility 단계가
`duplicate section anchor`로 종료됐다. 동일한 H2 제목이 한 문서 안에 반복됐고, LLMWIKI가
`document ID + H1 + H2`로 section ID를 만들기 때문에 두 섹션이 같은 anchor를 받은 것이 원인이다.

### 해결과 검증

원본이나 변환 본문을 삭제하지 않고 Electron 입력 어댑터가 normalized 사본의 두 번째 이후 동일
H2에 `· 2`, `· 3` suffix를 붙인다. fence 안의 Markdown 예시는 건드리지 않으며 이미 사용된 suffix와도
충돌하지 않게 다음 번호를 찾는다. 이전 실행이 남긴 invalid input은 source SHA가 같더라도 감지해
doc2md 결과로 복구하고 revision을 올린다.

중복 H2 최초 정규화와 같은 SHA의 legacy invalid input 복구를 회귀 테스트로 추가했다. 원본 파일은
변경하지 않으며 정상적으로 고유한 heading은 그대로 보존한다.

같은 corpus의 대형 문서에서는 LLMWIKI overlap chunk가 canonical section 안에서 여러 번 나타나
`evidence text is not unique`도 재현됐다. normalized 사본의 H2 본문을 현재 LLMWIKI chunk 한도보다
작은 2,200자 이하 섹션으로 나누고, H1과 첫 H2 사이의 preamble도 첫 섹션 분할에 포함했다. 실제
31개 문서 corpus에서 sidecar health `ok`, agent available, 새 knowledge version 활성화와 파일 상태
31개 `done`을 확인했다.

## 2026-07-22 — 최초 대량 변환에서 화면이 `빌드를 시작하는 중`에 고정됨

### 맥락과 영향

통합 개발 실행에서 폴더를 등록하면 doc2md가 OCR을 수행하고 normalized input을 차례로 만들었지만,
온보딩 화면은 `roots.add` IPC가 끝날 때까지 고정 문구와 spinner만 표시했다. 실제 출력 파일이 늘고
doc2md process가 CPU를 사용 중이어도 사용자는 정지나 실패로 판단할 수 있었다. 개발 launcher도
Electron을 띄우기 전에 deep health를 기다려 첫 화면 표시를 불필요하게 늦췄다.

### 원인과 해결

최초 `원본 → doc2md → LLMWIKI → sidecar` 흐름이 장시간 IPC 하나 안에서 동작하는데 파일별 상태를
기존 `build:state:changed` 채널로 보내지 않았고, 렌더러도 로컬 `submitting` boolean만 보고 있었다.
또한 등록 완료 직후 legacy `build.trigger`를 다시 호출해 같은 사용자 동작에 두 빌드 경로가 연결됐다.

- input sync가 시작, 현재 상대 파일, 완료 수, 전체 수와 변환·재사용 결과를 callback으로 보고한다.
- AppServices가 scan, convert, assemble, sidecar readiness를 하나의 진행률로 변환해 renderer에 보낸다.
- 온보딩은 현재 단계의 `n/N`, 파일명, progress bar와 백분율을 표시하고 실패 메시지를 그대로 보여준다.
- 폴더 등록은 즉시 확정해 메인 화면으로 이동하고, sidecar가 `ready`가 될 때까지 채팅만 비활성화한다.
  실패는 메인 화면의 build 상태에 남겨 등록 화면에 사용자를 가두지 않는다.
- production 등록 경로에서 legacy build의 중복 호출을 제거했다.
- 개발 launcher는 빠른 기본 health 뒤 Electron을 먼저 띄우고 OCR 초기화는 background conversion에서
  진행한다. 실제 통합 smoke만 긴 단일 요청으로 deep health를 검증한다.

### 검증과 회귀 방지

- input sync 진행 이벤트의 `0/N → 현재 파일 → 완료 n/N` 순서를 단위 테스트로 고정했다.
- TypeScript main/renderer typecheck, Vitest 46개, Electron production build가 통과했다.
- 실제 통합 smoke는 production input sync를 직접 호출하므로 fixture-only 우회를 허용하지 않는다.

첫 OCR 실행 시간은 사용자 문서 형식, Paddle model cache와 CPU에 따라 여전히 길 수 있다. 취소를
doc2md 요청까지 전달하는 AbortSignal 계약과 파일별 예상 시간 표시는 후속 개선 범위다.

## 2026-07-22 — 신규 폴더는 등록되지만 최초 검색 corpus가 비어 있음

### 맥락과 영향

실제 네 저장소 smoke는 미리 만든 `source-md/REG-100001.md`를 복사한 뒤 sidecar를 시작하고
있었다. 반면 Electron의 `addRoot`는 원본을 DB에 색인한 뒤 sidecar와 legacy build를 각각
시작했으며, sidecar가 읽는 app data의 `source-md`에는 아무 입력도 만들지 않았다. 신규 사용자는
폴더 등록 성공 화면을 보더라도 최초 LLMWIKI build가 빈 corpus이거나 시작 실패가 되는 결함이다.

### 원인과 해결

테스트 fixture가 production의 누락된 `원본 → doc2md → normalized input` 단계를 대신하고 있었고,
Electron legacy builder와 sidecar native builder의 저장소 layout도 달랐다. 폴더 등록 경로에
다음 단일 흐름을 추가했다.

```text
원본 scan·SHA 고정
→ loopback doc2md POST /v2/convert
→ stable path-based document ID와 LLMWIKI 1.0 front matter 생성
→ normalized input 원자적 저장
→ sidecar WikiBuildService 최초 build·활성화
```

같은 source URI·SHA는 sidecar가 승인 작업 중 갱신한 normalized input을 보존하고, SHA가 바뀔
때만 revision을 올린다. rename은 ownership manifest와 원본 SHA로 기존 ID·revision 계보를 찾고
source URI만 canonical percent-encoding해 갱신한다. input 경로는 모든 쓰기 전에 realpath까지
비중첩 검증하며, app ownership manifest가 없는 non-empty 디렉터리는 거부하고 manifest에 기록된
파일만 정리한다. path transport가 원본 절대경로를 전달하므로 doc2md URL은 인증정보 없는 loopback
HTTP로 제한했다. production `current.json` writer는 sidecar 하나로 고정하고 Electron의 기존
builder는 mock·호환 경로로 제한했다.

### 검증과 남은 위험

- normalized input 단위 테스트: 최초 변환, 동일 SHA no-op, rename ID 계승, revision 증가, 소유 파일만
  stale 정리, 예약문자 URI, loopback·경로 중첩 제한
- sidecar 수명주기 단위 테스트: health 직후 종료를 `ready`로 오인하지 않고 dispose가 crash restart
  timer를 취소함
- fixture seeder 없이 production `input-sync.ts`를 직접 실행한 실제 doc2md→LLMWIKI→sidecar smoke 통과
- exact diff 승인, 새 build publish, Undo와 후속 publish 통과

Sidecar lifecycle은 start/restart/stop을 직렬화하고, 예약 restart를 generation으로 무효화하며,
child event가 현재 소유 process에만 영향을 주게 했다. Anthropic 키 삭제는 기존 process 종료 확인이
실패하면 성공으로 숨기지 않는다. dev/smoke wrapper도 종료 신호에서 process group을 내리고 강제
종료 뒤 실제 exit를 확인한다.

앱 밖에서 원본 파일이 바뀌는 watcher 경로의 자동 재변환은 sidecar 승인 실행과 동시에 같은 입력을
쓰지 않도록 writer lock/event 계약을 추가한 뒤 활성화해야 한다. 플랫폼별 standalone Python
artifact와 서명된 installer도 별도 release gate로 남아 있다.

## 2026-07-22 — 실제 통합 smoke에서 doc2md와 sidecar가 시작 전에 종료

### 맥락과 영향

`codegate-2026-local`에서 민규 doc2md, 성주 sidecar, 용휘 LLMWIKI를 실제 프로세스로 띄워
검색→승인→재빌드→Undo를 검증하려 했다. 기대 결과는 두 health check가 준비 상태가 된 뒤
Electron이 사용하는 HTTP 계약으로 전체 흐름이 완료되는 것이었다.

처음에는 doc2md가 import 단계에서 종료됐고, Python 3.12 환경으로 바꾼 뒤에는 sidecar가 설정
검증 단계에서 종료됐다. 앱에서는 엔진 상태가 `ready`에 도달하지 못해 검색과 변경 흐름 전체가
막히는 영향이다.

### 재현과 증거

```bash
pnpm integrations:smoke
uv pip install --python ../codegate-2026-api/.venv/bin/python \
  --editable ../codegate-2026-api/services/doc2md --dry-run
```

- 기존 converter 루트 `.venv`는 Python 3.14였고 `pyhwp2md`가 없어 `app.converters` import가
  실패했다.
- dry run은 `paddlepaddle`에 cp314 wheel이 없어 doc2md 의존성 해석이 불가능하다고 보고했다.
- Python 3.12에서 doc2md deep health는 성공했다.
- 이후 sidecar는 `local environment requires CODEGATE_BOOTSTRAP_DEMO=false`로 종료됐다.
- Backend `Settings`는 local mode에서 auth, demo bootstrap, knowledge mode, LLMWIKI 경로와 CORS
  allowlist를 함께 요구하지만 Electron supervisor는 auth와 environment만 전달하고 있었다.

### 원인과 해결

첫 원인은 지원되지 않는 Python 3.14 ABI와 불완전한 로컬 환경을 실행 기준처럼 사용한 것이다.
두 번째 원인은 Backend CLI가 인자를 파싱하기 전에 기본 ASGI app을 import한다는 사실을 Electron
supervisor 환경 계약에 반영하지 않은 것이다.

Converter는 저장소 내부 venv를 전제로 하지 않고 `uv run --isolated --python 3.12`로 실행하게
했다. Sidecar는 `buildSidecarEnvironment`에서 local runtime invariant와 실제 source, LLMWIKI,
doc2md 경로를 CLI import 전에 모두 전달한다. 외부 환경이나 `extraEnv`가 local 안전 경계를
production/demo 값으로 덮어쓰지 못하게 최종 값을 고정했다.

Python 3.14 환경에 일부 패키지만 추가하는 대안은 Paddle wheel이 없어 제외했다. doc2md deep
health를 생략하는 방법은 실제 변환 불능을 숨기므로 제외했다. Backend CLI import 구조 자체를
이번 통합에서 재편하는 방법은 다른 저장소의 public entrypoint 변경 범위가 커 supervisor 경계에서
해결했다.

### 검증과 회귀 방지

- `tests/sidecar-supervisor.test.ts`: local runtime 환경 계약 포함 5개 통과
- doc2md v2/security/structure 집중 테스트: 54개 통과
- `pnpm integrations:smoke`: doc2md deep health, sidecar+LLMWIKI health, `REG-100001` 검색,
  exact diff 승인과 새 version publish, Undo와 원본 복구 통과
- smoke 종료 뒤 `services/doc2md/.venv`가 생성되지 않음을 확인

`buildSidecarEnvironment` 단위 테스트와 실제 네 저장소 smoke를 함께 유지한다. 단위 테스트는 필수
환경 누락을 빠르게 잡고, smoke는 import 순서·Python ABI·HTTP·build publication을 함께 잡는다.
부모 환경은 PATH·locale·임시/인증서 경로만 allowlist하고 child HOME을 app data root로 격리한다.
Supabase key를 포함한 그 밖의 부모 환경변수는 sidecar에 상속하지 않는 회귀 테스트를 유지한다.

### 남은 위험과 면접 질문

첫 isolated 실행은 dependency와 OCR model cache 상태에 따라 느릴 수 있다. 실제 Anthropic session,
서명된 설치본의 bundled sidecar/doc2md, 임의 사용자 corpus의 최초 대량 변환은 별도 설치본 E2E가
필요하다.

- 왜 단위 테스트가 통과했는데 실제 프로세스가 실패했나? spawn 인자만 검사했고 Backend의 import
  시점 설정 검증과 Python wheel ABI를 실행하지 않았기 때문이다.
- 왜 local venv를 커밋하지 않나? interpreter와 native wheel은 플랫폼별 산출물이며 소스·lock·재현
  명령만 버전 관리해야 하기 때문이다.
- 왜 health를 완화하지 않았나? 실행 가능하다는 신호가 실제 변환·검색 가능성을 보장해야 UI가
  거짓 `ready` 상태를 표시하지 않기 때문이다.
