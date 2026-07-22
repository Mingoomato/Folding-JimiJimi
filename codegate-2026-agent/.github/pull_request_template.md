## 변경 내용


## 변경 이유


## 검증

- [ ] `uv run ruff format --check .`
- [ ] `uv run ruff check .`
- [ ] `uv run mypy src`
- [ ] `uv run pytest`

## 보안·통합 확인

- [ ] secret과 실제 기업 문서를 포함하지 않음
- [ ] 승인 전 write 금지와 기존 역할 경계를 유지함
- [ ] 공개 API 또는 event 변경을 통합 계약에 반영함
