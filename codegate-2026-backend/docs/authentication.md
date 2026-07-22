# 인증 결정

## 결정

CODEGATE는 Google OAuth 2.0 Authorization Code flow와 PKCE를 직접 사용한다. Electron은 시스템
브라우저와 loopback callback을 소유하고, `cloud-api`는 authorization URL 생성, code 교환,
Google ID token 검증과 CODEGATE session 발급을 소유한다.

인증과 권한의 source of truth는 다음처럼 분리한다.

- Google: 사용자 신원과 verified email
- Cloud API: 1회용 OAuth flow, CODEGATE session, 향후 구독·권한
- Electron: 시스템 브라우저 실행, loopback callback, session token의 OS 보안 저장소 보관
- Local runtime: 로그인 token을 받지 않는 loopback filesystem capability

Google access token과 refresh token은 Google API를 호출하지 않는 현재 제품 범위에서 저장할
이유가 없으므로 code 교환 직후 폐기한다. CODEGATE session은 random opaque token이며 DB에는
HMAC digest만 저장한다.

## 보안 불변식

- OAuth state와 nonce는 backend가 생성하며 짧게 만료되고 한 번만 사용할 수 있다.
- PKCE는 `S256`만 허용한다.
- redirect는 포트가 있는 IPv4·IPv6 loopback HTTP URI만 허용한다.
- ID token은 Google JWKS로 signature, issuer, audience, expiry, nonce를 검증한다.
- verified email이 아닌 계정은 session을 만들지 않는다.
- Refresh token은 매 사용 시 회전하고 이전 access token도 함께 폐기한다.
- 사용 완료 refresh token의 재사용이 감지되면 같은 token family 전체를 폐기한다.
- OAuth start는 redirect 길이, client IP 호출량과 전체 pending flow 수를 제한한다.
- Refresh는 client IP 호출량과 token family별 총 rotation 수를 제한한다.
- OAuth client credential, session pepper, session token은 로그·Git·문서에 기록하지 않는다.

## 검토한 대안

- Supabase Auth: 제품 결정에 따라 사용하지 않는다. 외부 session 계층과 custom claims에 대한
  운영 의존성을 없애고 Google OAuth와 자체 session 경계를 명시적으로 소유한다.
- Google token을 CODEGATE Bearer token으로 사용: audience가 Google client이며 제품의 session
  폐기·회전 정책을 통제할 수 없어 제외했다.
- Electron에 Google client secret 포함: Desktop OAuth client는 public client이고 배포된 secret은
  비밀로 유지할 수 없으므로 인증 근거로 사용하지 않는다.
- Embedded webview 로그인: Google 정책과 피싱 방지 관점에서 시스템 브라우저보다 불리해
  제외했다.

만료된 session과 사용 완료 token digest는 인증 DB 시작, 상태 변경, 주기적 background
cleanup에서 정리하며, 유효한 session이 하나도 없는 사용자의 profile PII도 삭제한다.

현재 SQLite adapter는 하나의 deployment가 공유하는 파일과 transaction 경계를 전제로 한다.
여러 replica가 각자 다른 SQLite를 쓰면 OAuth state와 session이 분리되므로 지원하지 않는다.
수평 확장 전에는 동일한 consume·rotation 원자성을 제공하는 공유 PostgreSQL adapter가
필요하다.

운영 전에는 Google OAuth consent screen, Desktop client, 테스트 사용자·게시 상태와 gateway
rate limit을 실제 환경에서 검증해야 한다.
