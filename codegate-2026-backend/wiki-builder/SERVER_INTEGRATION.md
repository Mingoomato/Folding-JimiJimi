# 서버 연동 계약

이 패키지는 HTTP 서버가 아니라 문서 서비스가 호출하는 빌드 엔진이다. 인증된 서버 코드만
`WikiBuildService`를 호출해야 하며, 클라이언트가 전달한 경로·`tenant_id`·
`synthetic_corpus` 값을 그대로 신뢰해서는 안 된다.

## 권장 처리 흐름

1. 업로드 서비스가 원본 파일을 보관하고 SHA-256을 계산한다.
2. 변환 서비스가 별도 테넌트 영역에 입력 계약을 만족하는 Markdown 세트를 원자적으로
   저장한다. 의미 청크 모드라면 같은 `id`의 조각 전체를 한 세대로 게시한다.
3. 작업 큐가 해당 테넌트·위키의 현재 `build_id`를 읽어 작업에 기록한다.
4. 변경된 논리 문서 ID만 Gemini enrichment한다. 변경 파일의 `chunk_no`가 아니라 그
   front matter의 `id`를 작업 키로 사용한다.
5. 전체 파생 데이터를 새 불변 빌드로 만든다.
6. 작업 시작 시 기록한 `build_id`와 현재 값이 같은 경우에만 새 빌드를 활성화한다.
7. 활성화된 `current.json`을 검색 계층이 읽고 새 검색 인덱스를 교체한다.

같은 `tenant_id`와 `wiki_id`의 enrichment 작업은 작업 큐에서 직렬화하는 것을 권장한다.
빌드 설치와 current 교체는 파일 잠금 및 원자적 rename으로 보호되지만, 작업 직렬화는 중복
Gemini 호출 비용도 방지한다.

의미 청크 하나만 변경되어도 같은 `id`의 집계 SHA-256이 바뀌므로 해당 논리 문서의 기존
enrichment는 자동으로 stale 처리된다. 조각을 하나씩 비원자적으로 게시하면 빌더가 서로 다른
개정의 front matter를 함께 읽고 입력을 거부할 수 있으므로, 임시 디렉터리 완성 후 디렉터리
단위 교체를 사용한다.

## 낙관적 동시성 제어

`expected_current_build_id`를 요청에 넣으면 요청 시작 이후 다른 빌드가 활성화된 경우
`UnsafeOutputError`가 발생하고 새 빌드는 current가 되지 않는다. 최초 빌드임을 조건으로
하려면 `None`을 명시하고, 조건 없이 로컬 개발용으로 실행할 때만 필드를 생략한다.

```python
status = service.current_status(request)
expected = None if status["current"] is None else status["current"]["build_id"]

guarded_request = WikiBuildRequest(
    tenant_id=request.tenant_id,
    wiki_id=request.wiki_id,
    input_dir=request.input_dir,
    storage_root=request.storage_root,
    actor_id=request.actor_id,
    synthetic_corpus=False,
    expected_current_build_id=expected,
)
result = service.rebuild_documents(guarded_request, {"DOC-000001"})
```

## 오류 매핑 예시

- `ValidationError`: 잘못된 입력 또는 미승인 결과, HTTP 400/422
- `UnsafeOutputError`: 경로·불변성·current 충돌, HTTP 409
- `ProviderError`: Gemini 실패, 재시도 가능한 작업 실패 또는 HTTP 502/503

HTTP 요청 시간 안에 Gemini 전체 처리를 기다리지 말고 작업 ID를 반환하는 비동기 작업으로
운영한다. 재시도 시에는 같은 입력과 같은 설정이 동일한 `build_id`를 만들므로 이미 설치된
빌드를 재사용할 수 있다.

## 운영 경계

- API 키는 Secret Manager에서 프로세스 환경변수로 주입한다.
- 실제 비공개 문서는 검증된 유료 데이터 정책 설정에서만 처리한다.
- `restricted` 문서는 이 빌더에서 외부 LLM 전송을 차단한다.
- 검색·다운로드 계층은 manifest의 `access`에 따라 별도로 권한을 검사한다.
- 입력 경로는 서버가 생성한 테넌트 전용 경로만 사용한다.
- 현재 파일 잠금 구현은 단일 서버 또는 잠금을 공유하는 POSIX 파일시스템용이다. 여러 서버와
  객체 저장소를 사용할 때는 DB 트랜잭션/ETag 조건부 갱신으로 current 저장소를 교체한다.
- 이전 불변 빌드에는 삭제 전 문서가 남을 수 있으므로 보존 기간과 완전 삭제 절차를 서비스
  정책으로 정한다.
- 가격표와 모델 접근성은 배포 시점에 다시 확인하고 설정을 갱신한다.

사용자 인증, 업로드, 변환, 작업 큐, 검색 API, 자연어 편집 승인과 완전 삭제는 상위 서비스의
책임이다.
