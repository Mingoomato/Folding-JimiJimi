# Integration changes

- 네 저장소를 원래 상대 경로 계약을 보존한 채 한 루트에서 설치·실행·검증하도록 결합했다.
- Google OAuth cloud-api를 Electron과 함께 기동하고, OAuth callback을
  `http://127.0.0.1:47821/auth/callback`으로 고정했다.
- cloud-api 전용 client secret/session pepper를 Electron과 Python agent 환경에서 제거했다.
- Gemini chat provider를 추가하고 기본 모델을 `gemini-2.5-flash-lite`로 맞췄다.
- 내부 문서의 외부 전송은 `paid-no-training` 정책을 명시한 경우에만 허용한다.
- OAuth state 누락/불일치를 거부하고 cloud 권한 검사를 복구했다.
- 조용히 실패하던 다중 폴더 UI를 한 번에 한 폴더만 허용하는 fail-safe 동작으로 정리했다.
- doc2md 캐시 키를 실제 내용 해시로 바꾸고, 쓰기 상태를 `DOC2MD_DATA_ROOT` 아래로 옮겼다.
- convert → local-runtime → wiki-builder frontmatter 계약을 호환시켰다.
- LLMWIKI의 Gemini 2.5 설정, thinking budget, doctor, 가격 메타데이터를 일치시켰다.
- 첫 색인과 변경 재색인 전에 Gemini enrichment를 자동 실행하고, 입력·모델·프롬프트 기반 캐시와
  데이터 정책 검사를 그대로 적용하도록 Agent와 LLMWIKI 서비스 경계를 연결했다.
- Gemini enrichment가 검증 실패 목록을 반환해도 후보 공개를 중단하고, 이전 승인 캐시는 Undo
  복구용으로 보존한다. 느린 provider 호출은 전역 source-write barrier 밖으로 옮기고 활성 버전과
  source를 공개 직전에 다시 검증하며, `Retry-After`는 최대 30초로 제한했다.
- launcher의 환경변수 분리를 실행 검사로 고정하고, signal 종료 race와 nested detached process
  정리 유예시간을 수정했다. check/smoke/dev의 내부 pnpm 재호출도 설치된 바이너리 직접 실행으로
  바꿔 supply-chain 검사의 중복 실행을 제거했다.
- 고정 callback과 client secret 계약에 맞춰 Google OAuth 설정을 Web application client와 정확한
  Authorized redirect URI로 통일했다.
- 호출되지 않던 PowerPoint COM 렌더 경로와 직접 pywin32 의존성을 제거하고 PPT/PPTX capability 및
  문서를 실제 변환 경로와 맞췄다.
