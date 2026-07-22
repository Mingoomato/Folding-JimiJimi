# Work log

## 2026-07-22 — Railway 웹 프런트 배포 준비

- 목표: Electron 제품 UI를 훼손하지 않고 Railway에서 확인 가능한 브라우저 데모를 제공한다.
- 변경: 공용 preload API 타입 분리, 웹 메모리 bridge, 브라우저 전용 Vite build, 보안 정적 서버,
  multi-stage Dockerfile, Railway healthcheck를 추가했다.
- 영향: renderer, preload 타입 계약, 빌드·배포 설정, 공개 문서.
- 검증: typecheck, Vitest 55개, Electron production build, web production build, Docker build,
  container health·SPA fallback·보안 헤더를 실제 실행했다. opt-in 통합 테스트 1개는 제외됐다.
- 결정: 웹은 명시적 UI 데모이며 문서 내용·API 키를 서버로 보내지 않는다. 실제 분석·수정은
  Electron과 로컬 sidecar가 계속 소유한다.
- 배포 결과: Railway `codegate-frontend/frontend` production 배포가 성공했고
  `https://frontend-production-a058.up.railway.app`에서 원격 `/health`, SPA fallback, 보안 헤더와
  Chrome 초기 화면 렌더링을 확인했다.
