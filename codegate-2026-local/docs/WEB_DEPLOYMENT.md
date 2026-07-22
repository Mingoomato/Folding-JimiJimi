# Railway 웹 배포 결정 기록

날짜: 2026-07-22

## 결정

Railway에는 `codegate-local` Electron 앱 자체가 아니라 renderer의 브라우저 데모를 별도 빌드해
배포한다. `src/renderer/src/web/bridge.ts`가 Electron preload와 같은 `CodegateApi` 계약을
메모리에서 구현하며, 브라우저와 Electron이 같은 화면 컴포넌트를 공유한다.

웹 데모는 실제 로그인, API 키 저장, 문서 내용 변환, Claude 호출, 원본 변경을 수행하지 않는다.
지원 브라우저의 directory picker를 쓰더라도 파일명과 트리 상태만 브라우저 메모리에 유지한다.
브라우저가 directory picker를 지원하지 않으면 공개 샘플 파일명을 사용한다.

## 선택 이유

- Railway 컨테이너는 Electron `BrowserWindow`, OS 폴더 선택기, 로컬 Python sidecar를 사용자
  컴퓨터에 제공할 수 없다.
- renderer를 정적 웹으로만 서빙하면 `window.codegate`가 없어 첫 화면에서 로딩이 끝나지 않는다.
- 웹 데모 경계를 명시하면 실제 제품의 로컬 문서·승인·백업 보장을 훼손하지 않으면서 UI를
  공개적으로 검증할 수 있다.
- 공용 `CodegateApi` 타입을 Electron 구현과 분리해 두 런타임의 계약 드리프트를 타입검사로
  차단한다.

## 기각한 대안

- Electron 번들을 Railway에서 직접 실행: 원격 컨테이너의 창과 파일시스템을 사용자에게 제공할
  수 없어 제품 흐름이 성립하지 않는다.
- renderer 산출물만 정적 호스팅: preload 부재로 `불러오는 중`에 멈춘다.
- Anthropic 키를 브라우저에 주입: 공개 번들에서 서버 비밀을 노출하므로 기각했다.
- 웹 데모에서 실제 문서 수정을 흉내 내기: 데스크톱의 승인·백업·Undo 보장과 혼동되므로 기각했다.

## 운영 계약

- `pnpm build:web`은 `dist/web`만 생성한다.
- `scripts/web-server.mjs`는 `$PORT`에서 SPA fallback과 `/health`를 제공한다.
- 응답에는 CSP, `nosniff`, frame 차단, referrer·permissions 정책을 설정한다.
- `.dockerignore`는 `.env`와 로컬 통합 키가 build context에 들어가지 못하게 한다.
- 실제 제품 기능은 Electron 빌드와 `pnpm dev:integrated` 경로가 계속 소유한다.

현재 production 서비스는 Railway `codegate-frontend/frontend`이며 공개 주소는
`https://frontend-production-a058.up.railway.app`이다.

## 검증

- `pnpm typecheck`
- `pnpm test`: 55개 통과, opt-in 통합 테스트 1개 제외
- `pnpm build`
- `pnpm build:web`
- `docker build -f Dockerfile.web -t codegate-web:railway-test .`
- 컨테이너 `/health` 응답과 SPA fallback·보안 헤더 확인

## 남은 범위

실제 웹 제품이 필요해지면 Supabase PKCE, 서버 보관 Anthropic 키, 업로드·변환 API, tenant별 저장소,
삭제 정책을 별도 위협 모델과 함께 설계한다. 그 전까지 웹 배포는 명시적인 UI 데모다.
