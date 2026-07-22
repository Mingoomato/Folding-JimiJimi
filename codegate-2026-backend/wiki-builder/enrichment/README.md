# 합성 enrichment fixture

`approved/`의 JSON 파일은 `samples/input/` 50개 합성 문서로 생성한 개발·회귀 테스트용
fixture다. 실제 사용자 문서나 운영 데이터가 아니다.

운영 enrichment는 `wiki-storage/tenants/{tenant_id}/wikis/{wiki_id}/enrichment/`에 저장하며
Git에 커밋하지 않는다. 이 fixture를 변경할 때는 원문 해시, 모델 fingerprint, grounding 검증과
전체 테스트를 함께 확인한다.
