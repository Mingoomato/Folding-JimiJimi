# ADR 0004: 로컬 Agent·LLMWIKI와 로그인 권한 경계

- 상태: 채택
- 날짜: 2026-07-21

## 배경

배포된 프로그램은 사용자가 허용한 로컬 파일을 읽고 수정해야 한다. Claude Agent SDK와
LLMWIKI도 사용자 장치에서 실행하되, 모델 호출은 Anthropic API로 나가며 로그인은 팀의
Supabase Auth를 사용한다. 로컬 파일 권한, 로그인한 사용자 신원, LLMWIKI의 활성 지식 버전은
서로 다른 보안·일관성 경계다.

초기 계약은 `LLMWIKI@fb0f8c7`과 `codegate-2026-api@360ff41`의 doc2md v0.1.0을 기준으로
결정했다. 2026-07-22 재검증한 현재 integration pin은 `LLMWIKI@acf94f7`과 이름이 바뀐
`codegate-2026-convert@30e6814`의 doc2md v0.2다. `server-v2` 쓰기 범위는
[ADR 0005](0005-llmwiki-server-v2-write-compatibility.md)에서 별도로 결정한다.

## 결정

원본 로컬 파일을 업무 데이터의 단일 정본으로 둔다. doc2md가 만든 정규화 Markdown은
LLMWIKI 입력이며 파생 데이터다. 승인된 원본 변경 뒤 정규화 Markdown의 본문,
`source.sha256`, revision을 함께 갱신하고 `WikiBuildService.build`를 in-process로 호출한다.
LLMWIKI의 tenant/wiki `current.json`만 활성 지식 포인터다. Backend 형식으로 변환한 산출물은
native build ID에 결박된 checksum 포함 read cache이며 별도 활성 포인터를 만들지 않는다.

Claude Agent SDK process, session state와 app-owned MCP 도구는 로컬에서 실행한다. SDK의
built-in Read/Edit/Write/Bash/Web 도구는 허용하지 않는다. 모델은 ACL이 적용된 문서 ID로만
LLMWIKI 정본과 현재 UTF-8 원본을 읽을 수 있다. 쓰기는 모델 도구가 아니라 기존 exact diff,
`plan_hash`, 사용자 승인, backup, atomic replace, outbox, Undo 경로만 사용한다. Claude 추론은
Anthropic API 호출이므로 로컬-only 또는 오프라인이라고 표현하지 않는다.

로그인은 frontend가 Supabase Auth와 직접 수행한다. Backend는 password, refresh token,
service-role key를 받거나 저장하지 않고 asymmetric JWKS로 access token을 검증한다.
`GET /api/v1/auth/me`는 UI가 사용할 정규화된 신원·권한 snapshot만 `no-store`로 반환한다.
Custom Access Token Hook의 access row가 있는 사용자만 `codegate_provisioned=true`이며, 이 값이
없으면 signed write document claim이 있어도 쓰기를 허용하지 않는다.

다운로드 앱의 로컬 sidecar 권한은 Supabase 신원과 분리한다. sidecar는 loopback에만 bind하고
명시적 CORS origin만 받으며, 겹치지 않는 source/input/storage root만 사용한다. 로컬 파일
capability는 OS에서 사용자가 선택한 workspace 범위에서 생긴다. Supabase 로그인만으로 다른
로컬 폴더 권한을 얻지 않는다. Frontend는 cloud API용 Bearer client와 local sidecar client를
분리한다.

## 결과

- 원본, 정규화 입력, native active build, compatibility cache의 책임이 한 방향으로 흐른다.
- LLMWIKI build 또는 CAS activation이 실패하면 이전 `current.json`이 유지되고 outbox retry/Undo가
  가능하다. Native activation 뒤 compatibility 변환이 실패한 경우에는 process가 이전 loaded
  repository를 유지하지만 native pointer는 이미 바뀌므로, 배포 전 pinned-corpus smoke와 호환
  adapter 갱신이 restart 안전 조건이다.
- 외부 build가 CAS에서 먼저 이기거나 publish 뒤 process가 중단돼도 native current를 다시
  읽고 이미 반영된 event를 멱등 완료할 수 있다.
- source, LLMWIKI input, storage는 서로 겹칠 수 없다. 특히 source SHA를 포함하는 정규화
  Markdown을 그 자신의 source로 쓰는 순환 구성을 금지한다.
- LLMWIKI bundle은 Python 코드로 실행되는 신뢰 구성요소다. 다운로드 앱은 검토·고정된 bundle을
  제공해야 하며 사용자가 업로드한 임의 project root를 실행해서는 안 된다.

## 검토한 대안

- Claude built-in 파일 도구: 승인·ACL·Undo 경계를 우회하므로 제외했다.
- cloud agent가 사용자 파일에 직접 접근: 로컬 filesystem capability를 안전하게 전달할 수 없어
  제외했다.
- Backend용 두 번째 `CURRENT`: native LLMWIKI와 split-brain이 생겨 build ID cache로 대체했다.
- Backend password 로그인 endpoint: Supabase session 수명주기와 secret 보관을 중복해 제외했다.
- Supabase JWT를 로컬 폴더 권한으로 사용: cloud identity와 OS capability가 결합돼 제외했다.
- source와 정규화 입력의 같은 경로 사용: 자기 자신의 SHA를 frontmatter에 넣는 순환과 부분
  갱신 위험 때문에 금지했다.

## 검증 조건

- 실제 LLMWIKI checkout으로 initial build와 Backend cache load 성공
- 로컬 명령 → preview → approve → source atomic write → normalized Markdown 갱신 → native
  `current.json` 교체 → 새 근거 검색 성공
- native schema, build metadata, source hash, canonical evidence와 checksum 검증
- symlinked native artifact와 겹치는 local root 차단
- valid/invalid Supabase JWT, 미등록 사용자 write 차단, `/auth/me` no-store 계약
- non-loopback local API 요청과 credentialed CORS 차단
