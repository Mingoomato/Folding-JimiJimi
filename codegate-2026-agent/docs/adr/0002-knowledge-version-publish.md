# ADR 0002: 원본 참조와 지식 버전 원자적 공개

- 상태: 채택
- 날짜: 2026-07-21

## 결정

지식 그래프 산출물은 완전한 `llm-wiki` 디렉터리를 graph version마다 새로 만든다. 원본 파일을 이 디렉터리로 복사하거나 OS shortcut·symlink를 만들지 않고, manifest의 stable `document_id`, `source.uri`, `source.sha256`로 참조한다. `source://<root-id>/<relative-path>`는 FastAPI의 allowlisted resolver만 실제 경로로 바꾼다.

파일 수정 이후 파이프라인은 다음 의존 순서를 따른다.

```text
source compare-and-swap + atomic replace
→ file_version·audit·outbox commit
→ 해당 문서 증분 변환
→ FTS / vector / graph links 병렬 갱신
→ schema·reference·checksum barrier
→ 새 graph VERSION 원자적 공개
→ 검색 adapter 전환
```

독립적인 FTS, vector, graph link 갱신은 변환 결과가 나온 뒤 병렬 실행한다. 하나라도 실패하면 이전 graph version을 계속 제공하고 실패한 작업만 idempotent하게 재시도한다. 부분 갱신을 `latest`로 공개하지 않는다.

같은 문서의 동시 변경은 계획의 `base_sha256` compare-and-swap과 문서별 execution lock으로 직렬화한다. 다른 문서의 변환·index 산출은 병렬 처리할 수 있지만 graph version 공개는 단일 coordinator가 이전 active version을 기준으로 compare-and-swap한다. CAS에 진 작업은 같은 stale package를 재시도하지 않는다. 새 active version을 base로 아직 반영되지 않은 document event를 rebase하거나 대기 event를 하나의 새 package로 coalesce한 뒤 다시 검증한다. Undo도 새 변경 이벤트이며 과거 package를 직접 덮어쓰지 않는다.

## 이유

- OS shortcut과 absolute path는 실행 환경에 종속되고 symlink는 root escape 위험을 만든다.
- source URI와 hash는 원본 위치, 권한, 변경 충돌을 backend가 결정적으로 검증할 수 있게 한다.
- fan-out 작업을 병렬화하면 지연을 줄이면서도 publish barrier가 혼합 version 노출을 막는다.
- immutable version은 실패 복구, rollback, audit와 데모 재현을 단순하게 만든다.

## 검토했지만 선택하지 않은 대안

- 원본 파일을 `llm-wiki`에 복사: 원본과 복사본 중 어느 것이 최신인지 불명확해지고 민감 파일이 저장소에 섞일 수 있다.
- 문서별 symlink 또는 `.lnk`: 운영체제·호스트 경로에 종속되고 경로 이탈과 끊어진 링크 처리가 어렵다.
- 수정 직후 모든 index를 독립적으로 즉시 활성화: 일부 작업 실패 시 FTS, vector, graph가 서로 다른 내용을 반환한다.
- 매 수정마다 전체 rebuild만 수행: 가장 단순하지만 문서 수가 늘면 데모 응답 시간이 불필요하게 길어진다. 증분 update 실패 시 fallback으로 남긴다.

## 후속 구현

- `DocumentContentChanged`, `DocumentConverted`, `GraphUpdated` outbox event와 idempotency key
- 문서별 lock, base hash conflict, publish compare-and-swap
- CAS 패자의 새 active version rebase, 대기 event coalescing과 bounded retry
- 병렬 fan-out과 retry/dead-letter 상태
- active graph version의 원자적 전환과 in-flight request snapshot 보장
- 변경·변환·색인·공개 상태를 구분한 execution API와 UI
