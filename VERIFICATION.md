# 통합 검증 기록

검증일: 2026-07-22 (Asia/Seoul)

## 결과

`pnpm check` 전체 통과:

- Electron: TypeScript typecheck 통과, 132개 테스트 통과·1개 opt-in 테스트 건너뜀, production build 통과
- Agent: Ruff·mypy 통과, 231개 테스트 통과
- Google OAuth cloud-api: Ruff 통과, 22개 테스트 통과
- LLMWIKI builder: Ruff 통과, 52개 테스트 통과
- 구형 local-runtime 계약: Ruff 통과, 14개 테스트 통과
- doc2md: 106개 테스트 통과

`pnpm smoke` 전체 통과:

1. 원본 문서 → doc2md → LLMWIKI normalized input
2. Agent sidecar + LLMWIKI health
3. HTTP 계약을 통한 문서 위치 검색
4. exact diff 승인 → 원본 변경 → 새 LLMWIKI 버전 공개
5. Undo → 원본 복구 → 후속 LLMWIKI 버전 공개

Gemini enrichment 연결은 별도 단위 테스트로 다음 경계를 확인했다.

- API 키 없음: provider 호출 없이 오프라인 불완전 색인으로 폴백
- API 키 있음: 기존 개인정보 정책·캐시·scope 경계에 위임
- 잘못된 `CODEGATE_GEMINI_DATA_POLICY`: 설정 단계에서 거부
- enrichment 실패: stage/activation을 진행하지 않음
- stage 성공: enrichment가 먼저 실행되고 후보가 자동 활성화되지 않음
- 실패한 새 enrichment가 이전 승인 캐시를 지우지 않아 Undo 시 복구 가능
- 느린 provider 호출은 전역 source-write publish barrier 밖에서 실행하고, 활성 버전과 source를
  barrier 안에서 다시 검증
- 과도한 `Retry-After`는 30초로 제한

## 환경 진단

`pnpm doctor`에서 Node.js, pnpm, uv, 모든 설치 경로, session pepper, 모델, 데이터 정책과 아래
callback이 정상으로 확인됐다.

```text
http://127.0.0.1:47821/auth/callback
```

현재 실패 항목은 사용자가 입력하기로 한 다음 세 값이 비어 있는 것뿐이다.

```dotenv
GEMINI_API_KEY=
CODEGATE_GOOGLE_CLIENT_ID=
CODEGATE_GOOGLE_CLIENT_SECRET=
```

따라서 자격 증명 입력 뒤 `pnpm doctor`를 다시 실행해야 한다. 자격 증명이 제공되지 않았으므로 실제
Google 로그인과 실제 Gemini 네트워크 호출은 실행하지 않았다. 나머지 통합 smoke는 외부 서비스 없이
결정적 fixture로 검증했다.

## 추가 점검

- `.env` 권한: 소유자만 읽기·쓰기 가능 (`0600`)
- 소스 영역에서 Google API key, OAuth client secret, private key 형태의 값 없음
- OAuth client secret과 session pepper는 cloud-api에만 전달되고 Electron·Agent·doc2md에서는 제거됨
- Gemini key는 Agent/LLMWIKI에만 전달되고 cloud-api에는 전달되지 않음
- signal로 먼저 종료된 Electron 감지 회귀 테스트 통과; nested supervisor 정리 유예시간은 내부
  최대 정리시간 10초보다 긴 15초로 설정
- root launcher 환경 경계 검사 통과: Google secret/pepper와 Gemini key가 서로 다른 프로세스로 분리됨
