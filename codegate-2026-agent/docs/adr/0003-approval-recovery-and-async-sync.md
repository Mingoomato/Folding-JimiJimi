# ADR 0003: exact 승인, crash recovery, 비동기 지식 동기화

- 상태: 채택
- 날짜: 2026-07-21

## 결정

파일 변경 계획은 `ReplaceExact` 하나로 제한하고, schema version, 문서·파일 version, source URI, base/result SHA-256, typed operation, 화면에 표시한 exact diff, 생성·만료 시간을 domain-separated canonical JSON으로 hash한다. owner는 tenant·subject scoped persistence와 idempotency record로 결박한다. approve는 저장된 plan을 다시 hash하고 같은 principal, 미만료, pending 상태, 최신 ACL, 현재 source hash를 모두 확인한다.

파일과 SQLite를 하나의 transaction으로 묶을 수 없으므로 write journal을 먼저 durable하게 저장하고 immutable backup을 만든다. source root 아래 경로는 dirfd와 `O_NOFOLLOW`로 열고, 같은 directory의 temp file을 fsync한 뒤 `os.replace`와 directory fsync를 수행한다. 재시작 시 before/after hash로 unfinished journal을 복구한다. 쓰기 오류 뒤 source가 before hash면 journal을 aborted로 기록하고, 같은 approve/Undo 재요청에서 기존 execution을 다시 prepared로 만드는 방식으로 재개한다. Undo는 현재 hash가 원 실행의 after hash와 같을 때만 backup bytes를 새 execution으로 적용한다.

approve와 Undo HTTP 요청은 안전한 파일 적용과 outbox commit 뒤 `202 Accepted`와 execution `Location`을 반환한다. lifespan-owned worker가 문서 변환 뒤 FTS, vector, graph를 병렬로 만들고 schema·canonical evidence·reference·checksum barrier를 통과한 candidate만 expected parent version CAS로 공개한다. 실행 상태는 file과 sync로 분리하고 frontend는 polling한다. 실패 시 이전 knowledge snapshot을 계속 제공하며 retry-sync가 같은 event를 멱등 재처리한다.

로컬과 single-volume Railway MVP의 operational state는 repository port 뒤 SQLite를 사용한다. 한 process와 한 volume일 때만 지원한다. multi-replica로 확장하기 전에는 Postgres transaction/advisory lock과 외부 immutable artifact store로 교체한다.

## 이유

- 모델의 permission과 사용자의 업무 승인은 다른 보안 경계다.
- exact plan과 base hash는 preview 뒤 바뀐 내용이나 stale file을 덮어쓰는 일을 막는다.
- durable journal은 file replace와 DB commit 사이 crash gap을 복구한다.
- 긴 converter 호출을 HTTP lifecycle에서 분리하면 timeout과 중복 승인 위험을 줄인다.
- immutable snapshot과 request pinning은 FTS, vector, graph의 혼합 version 노출을 막는다.

## 검토한 대안

- Claude built-in Edit/Write: 승인된 diff와 실제 side effect를 application DB에서 증명할 수 없어 제외했다.
- approve 요청에서 operation 재제출: 화면과 다른 payload로 바뀔 수 있어 plan hash만 받는다.
- 변환 완료까지 approve 연결 유지: converter timeout이 사용자 재시도와 중복 write 위험을 키워 제외했다.
- WebSocket 우선: polling으로 필요한 상태가 모두 표현되므로 E2E 뒤 필요할 때 SSE/WebSocket을 추가한다.
- SQLite 다중 replica: in-memory/document flock와 local CURRENT CAS가 host 경계를 넘지 못하므로 금지한다.

## 검증 조건

- wrong, expired, rejected, reused plan은 write 0건
- 동시 stale plan 중 하나만 source CAS 성공
- symlink, traversal, unsupported format과 oversized file 차단
- replace 직후 crash에서 재시작 수렴
- fan-out 일부 실패 중 이전 snapshot 유지
- retry 뒤 새 immutable version 공개
- source 무변경 파일 쓰기 실패 뒤 같은 execution으로 approve·Undo 재개
- Undo byte-for-byte 복구와 외부 변경 conflict
