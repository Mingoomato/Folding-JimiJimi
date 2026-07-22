# ADR 0007: Railway Backend gateway와 private doc2md 분리

- 상태: 채택
- 날짜: 2026-07-22

## 배경

사용자에게는 Backend와 문서 변환 기능이 하나의 API로 보여야 한다. 동시에 PaddleOCR을 포함한
doc2md는 image와 memory가 크고 cold-start·변환 부하가 Backend API와 다르다. 별도 Railway service는
Backend volume을 공유하지 않으므로 path transport도 사용할 수 없다.

## 결정

같은 Railway project/environment에 public `backend`와 private `doc2md` 두 service를 배포한다.
Browser와 Frontend는 Backend domain만 호출한다. Backend는 `doc2md.railway.internal:8080`으로
authenticated `POST /v2/convert`를 호출하고 원본은 bounded bytes transport로 전달한다. 두 service는
같은 random Bearer secret을 환경변수로만 공유한다.

doc2md에는 public domain을 두지 않는다. Backend와 doc2md healthcheck, synthetic bytes 변환의 source
SHA·section mapping·converter version, Backend health의 converter readiness를 모두 통과해야 통합 배포가
완료된다.

## 결과

- 사용자와 Frontend의 endpoint는 하나이고 Converter 인증정보와 내부 주소는 노출되지 않는다.
- OCR failure, memory와 배포 주기를 Backend process에서 격리할 수 있다.
- service 간 base64 전송 비용과 두 container 운영 비용이 생긴다.
- 현재 32 MiB source 상한보다 큰 파일에는 object storage URI 또는 streaming transport가 필요하다.

## 검토한 대안

- 한 container에서 Backend와 doc2md 동시 실행: 단일 process supervisor와 장애 결합, 큰 image와 독립
  scaling 문제 때문에 제외했다.
- 별도 public Converter: 공격 표면과 두 공개 endpoint가 생겨 제외했다.
- private service에 path 전달: Railway service 간 filesystem namespace가 달라 동작하지 않는다.
- URI transport: object storage와 SSRF 정책이 준비되지 않아 현재 범위에서 제외했다.
