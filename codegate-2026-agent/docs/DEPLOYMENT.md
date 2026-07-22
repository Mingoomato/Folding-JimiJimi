# Supabase·Railway 배포 runbook

이 문서는 설정 계약이다. migration 적용, Auth hook 활성화, Railway volume 생성, production 배포는 실제 외부 환경을 바꾸므로 별도 확인 후 수행한다.

## 1. Supabase

1. `supabase/migrations/202607210001_codegate_access_token_hook.sql`을 검토해 적용한다.
2. `codegate_user_access`에 user의 tenant와 read scope를 넣는다.
3. `codegate_document_permissions`에 실제 write 가능 문서만 넣는다.
4. Supabase Dashboard의 `Authentication > Hooks`에서 Custom Access Token SQL hook을 `public.codegate_custom_access_token_hook`으로 활성화한다.
5. 새 token을 발급해 `codegate_provisioned`, `codegate_tenant_id`, `codegate_read_access`, `codegate_write_document_ids`를 확인한다.
6. Frontend가 그 access token으로 `GET /api/v1/auth/me`를 호출해 같은 tenant와 권한 snapshot을 받는지 확인한다.

Migration은 `supabase_auth_admin`에 schema usage, 두 ACL table의 select, hook execute만 주고 `anon`, `authenticated`, `public` 실행 권한은 회수한다. access row가 없는 user는 `codegate_provisioned=false`이고 Backend write gate를 통과하지 못한다. Supabase는 hook이 기존 필수 JWT claims를 보존하도록 요구하므로 함수는 원래 claims에 네 custom claim만 덧붙인다. [Supabase Auth Hooks](https://supabase.com/docs/guides/auth/auth-hooks), [Custom Access Token Hook](https://supabase.com/docs/guides/auth/auth-hooks/custom-access-token-hook)

JWT 권한은 발급 시점 snapshot이다. 해커톤 MVP는 access token 만료 시간을 짧게 두고 권한 변경 뒤 token refresh를 강제한다. 즉시 권한 회수가 필수인 운영 단계에서는 approve와 Undo 직전에 authoritative permission store를 재조회하는 adapter를 추가한다.

Backend는 Supabase service-role key를 사용하지 않는다. public JWKS로 user JWT signature·issuer·audience·expiry를 검증한다. [Supabase JWT](https://supabase.com/docs/guides/auth/jwts)

JWKS 조회는 짧은 timeout, known-key cache, unknown-`kid` negative cache와 최소 refresh 간격을 사용한다. Backend의 Authorization header/IP 제한은 단일 process 보호용이므로 production gateway에서도 IP·connection rate limit을 켠다. `CODEGATE_AUTH_RATE_LIMIT_REQUESTS`, `CODEGATE_AUTHORIZATION_HEADER_MAX_BYTES`, `CODEGATE_SUPABASE_JWKS_TIMEOUT_SECONDS`는 부하 테스트 뒤 조정한다.

## 2. Railway volume

Backend service에 persistent volume 하나를 `/data` 같은 절대경로로 연결한다. Railway는 volume이 연결되면 `RAILWAY_VOLUME_MOUNT_PATH`를 자동 제공하며, Backend는 source, SQLite, backup, locks, Claude state, immutable knowledge releases를 모두 그 아래로 매핑한다. [Railway Volumes](https://docs.railway.com/volumes)

현재 상태 저장과 file lock은 single-host SQLite·filesystem 기반이다. Railway volume은 replica와 함께 사용할 수 없으므로 한 Uvicorn process와 하나의 volume만 사용한다. 여러 replica가 필요해지면 operational state, lock, publish CAS를 Postgres/object storage adapter로 바꾸기 전에는 확장하지 않는다. [Railway volume 제약](https://docs.railway.com/volumes/reference)

### doc2md 배치 제약

Backend adapter는 doc2md v0.2 `POST /v2/convert`를 사용한다. 배치 방식에 따라 transport 하나를
선택한다.

1. **같은 filesystem namespace.** `CODEGATE_DOC2MD_SOURCE_KIND=path`로 두고 doc2md에 Backend
   source root를 `DOC2MD_ALLOWED_ROOTS`로 허용한다. 단순히 private URL만 연결한 별도 Railway
   service는 Backend volume을 볼 수 없으므로 이 구성으로 동작하지 않는다.
2. **별도 service.** `CODEGATE_DOC2MD_SOURCE_KIND=bytes`로 두고 승인된 원본을 inline base64로
   전송한다. Backend는 전송 직전 SHA-256과 최대 32 MiB 상한을 검사하므로 filesystem 공유가
   필요 없다.

두 구성 모두 `DOC2MD_API_TOKEN`과 `CODEGATE_DOC2MD_API_TOKEN`에 같은 Bearer secret을 넣는다.
`/health` 성공만으로 실제 path 또는 bytes 변환이 가능하다고 판정하지 않는다. doc2md의
`GET /health?deep=1`로 synthetic 변환과 OCR·보안 설정을 확인하고, 이어서 Backend가 선택한
transport로 synthetic source를 한 번 변환해야 배포 완료로 본다.

현재 Railway 데모는 같은 `codegate-backend` project의 `backend`와 `doc2md` 두 service로
구성한다. public domain은 Backend에만 연결하고 doc2md는
`http://doc2md.railway.internal:8080`에서만 접근한다. Uvicorn 기반 doc2md는 Railway private
network의 IPv4/IPv6를 함께 받도록 빈 host에 bind한다. 사용자는 하나의 Backend URL만 사용하지만
OCR process와 API process는 독립 배포해 자원과 장애를 격리한다. 이 선택의 근거는
[ADR 0007](adr/0007-railway-backend-doc2md-topology.md)에 기록한다.

## 3. 필수 환경변수

```text
CODEGATE_ENVIRONMENT=production
CODEGATE_AUTH_MODE=supabase
CODEGATE_AGENT_MODE=claude
CODEGATE_CLAUDE_MODEL=<exact-model-id>
CODEGATE_CLAUDE_SESSION_TTL_SECONDS=3600
CODEGATE_BOOTSTRAP_DEMO=false
CODEGATE_DOC2MD_BASE_URL=http://<doc2md-host>:<port>
CODEGATE_DOC2MD_SOURCE_KIND=path|bytes
CODEGATE_DOC2MD_API_TOKEN=<shared-doc2md-secret>
CODEGATE_SUPABASE_URL=https://<project-ref>.supabase.co
CODEGATE_SUPABASE_JWT_AUDIENCE=authenticated
CODEGATE_CORS_ORIGINS=["https://<vercel-domain>"]
ANTHROPIC_API_KEY=<server-secret>
```

`RAILWAY_VOLUME_MOUNT_PATH`는 volume 연결 시 Railway가 제공한다. 값이 없거나 mount가 writable하지
않거나 mutable path가 volume 밖이면 startup이 실패한다. production에서 disabled/demo auth,
deterministic agent, 누락된 Claude model ID, local demo bootstrap, 누락된 doc2md URL도 설정 검증에서
거부한다. Model ID를 바꿀 때는 prompt eval corpus를 다시 실행하고 기존 SDK session을 resume하지
않는다.

## 4. 첫 source와 knowledge package

`CODEGATE_BOOTSTRAP_DEMO=false`이므로 volume의 `${RAILWAY_VOLUME_MOUNT_PATH}/source`에 실제 허용 source tree가 있어야 한다. 또한 `${RAILWAY_VOLUME_MOUNT_PATH}/knowledge/releases`에 검증된 immutable release를 배치하고 `CURRENT`에 그 release directory 이름을 기록해야 한다. 둘 중 하나라도 없으면 production startup은 실패하며 image의 demo seed를 대신 공개하지 않는다. 실제 회사 문서와 absolute path는 image나 Git에 넣지 않는다.

## 5. 배포 후 확인

1. `/api/v1/health`의 auth mode, persistence, agent, converter, worker, knowledge version, `stale_sync_events=0`과 `index_artifacts_available=true`를 확인한다.
2. anonymous public 검색, valid/invalid JWT와 `/api/v1/auth/me`의 provisioned/unprovisioned 응답을 각각 확인한다.
3. synthetic Markdown 문서로 preview가 write 0건인지 확인한다.
4. 승인 후 `202 Location`을 polling해 `file_applied → syncing → completed`를 확인한다.
5. source hash, backup, 새 revision·section citation, graph version 변경을 확인한다.
6. vector stage failure를 주입할 수 있는 staging에서 이전 version 유지와 retry-sync를 확인한다.
7. Undo 후 원본 byte와 새 graph version을 확인한다.

데모 배포에서는 `/docs`가 외부 CDN 없이 렌더되는지, `/`가 `/docs`로 이동하는지도 확인한다.
Swagger UI asset CDN이 차단된 브라우저에서도 endpoint 목록과 OpenAPI JSON 링크가 보여야 한다.

실제 production migration, volume 변경, deployment와 restart는 자동 수행하지 않는다.
