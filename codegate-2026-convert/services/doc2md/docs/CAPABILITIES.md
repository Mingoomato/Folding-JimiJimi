# doc2md capability 표

`GET /capabilities`가 반환하는 것과 같은 내용이다. 실행 중인 서비스의 응답이 source of
truth이고, 이 문서는 계약 검토용 사본이다.

## 형식별 지원

| format | parse | ocr | page_mapping | **write_back** | library |
|---|:---:|:---:|:---:|:---:|---|
| pdf | ✅ | ✅ 내장 이미지 | ✅ | ❌ | markitdown (페이지 단위) + PaddleOCR |
| pptx / ppt | ✅ | ✅ 내장 이미지 | ✅ 슬라이드 번호 | ❌ | markitdown + PaddleOCR |
| hwpx | ✅ | ✅ 내장 이미지 | ❌ | ❌ | pyhwp2md + PaddleOCR |
| hwp (구형) | ✅ | ❌ 추출 불가 | ❌ | ❌ | pyhwp2md |
| docx / xlsx / csv / html / md / txt | ✅ | ❌ | ❌ | ❌ | markitdown |

## write_back이 전부 ❌인 이유

이 스택에 HWP·PDF·DOCX **writer가 없다.** markitdown, pyhwp2md, PaddleOCR은 모두 단방향
읽기 도구다. 원본 바이너리에 편집을 되쓰는 경로는 구현돼 있지 않고 검증된 적도 없다.

TEAM_HANDOFF의 "검증되지 않은 HWP/PDF write 지원 주장"은 보내지 않을 항목으로 명시돼 있고,
"검증된 writer가 없으면 Backend는 read-only로 유지합니다"가 그 결론이다. 따라서 이 서비스에
대해 문서 저장소는 **read-only로 유지**하면 된다. ✅로 바뀌는 일은 실제 writer가 생기고
round-trip 검증을 통과한 뒤에만 일어난다.

## 페이지 경계

PDF는 **페이지 단위로 변환**해 본문에 페이지 구분자를 남긴다.

```markdown
<!--- page 1 --->

# 입찰안내서
...

<!--- page 2 --->
...
```

HTML 주석이라 렌더링에는 보이지 않고 markdown 왕복에도 살아남으며,
`sections[].source_page`가 이 마커에서 채워진다. pptx는 슬라이드 번호가 같은 역할을 한다.

### 왜 페이지 단위로 다시 변환하는가

markitdown은 PDF를 한 덩어리 문자열로 만든다. 산문 경로는 `pdfminer.extract_text`를 파일
전체에 호출하고, form 경로는 페이지별 조각을 빈 줄로 이어붙인다. **둘 다 쓸 수 있는 페이지
경계를 남기지 않는다** — form feed 구분자는 산문 경로에서만 살아남았다(코퍼스 실측:
3페이지 PDF에 form feed 0개).

그래서 PyMuPDF로 **한 페이지짜리 PDF를 메모리에 만들어** markitdown에 한 장씩 넘긴다.
변환 기계는 그대로이므로 표 재구성이 손상되지 않는다:

| 문서 | 통짜 변환 | 페이지 단위 | 표 파이프 |
|---|---:|---:|---|
| 5페이지 (28K자) | 27,927자 | 27,927자 | 3,021 → **3,021** |
| 3페이지 | 8,978자 | 8,977자 | 837 → **837** |
| 353페이지 | 338,527자 | 360,146자 | 3,586 → **3,586** |

353페이지 문서에서 49.4초 → 111.3초(2.25배)가 든다. 전체 코퍼스는 114.4초 → 173.8초.
텍스트는 오히려 6.4% 늘어난다(한 장만 변환하면 블록이 옆 페이지와 병합되지 않아서).

### 시도했다가 되돌린 것

PyMuPDF의 `get_text()`로 직접 뽑으면 PDF 코퍼스가 66.3초 → 1.4초(47배)가 되고 한글
문자수도 완전히 동일하다(204,633 → 204,633). 그러나 **표가 전부 사라진다** — 28K자 문서
하나에서 표 210행·파이프 3,021개가 0이 된다. 표 보존이 이 파이프라인이 가장 공들인
부분이라 채택하지 않았다.

HWP/HWPX는 본문에 페이지 개념 자체가 없다 — 이건 선택이 아니라 형식의 성질이라
`source_page`가 항상 null이다.

## OCR 범위

OCR은 **그림 안에 구워진 글자만** 읽는다. 문서의 본문 텍스트는 각 형식의 변환기가 읽고,
그게 정본이다. 인식된 글자는 그림이 있던 자리에 삽입되며, 변환기가 이미 뽑은 줄과
겹치면(공백 무시 비교) 버려진다.

구형 `.hwp`만 예외로 이미지를 아예 읽을 수 없다 — 바이너리 CFB 컨테이너라 pyhwp2md가
이미지 데이터를 노출하지 않는다. 이 경우 `hwp_images_unsupported` diagnostic을 남긴다.

## OCR 요건

| 조건 | 없을 때 |
|---|---|
| PaddleOCR + paddlepaddle | 이미지 텍스트 없이 변환, `ocr_engine_unavailable` diagnostic |
| CUDA GPU | CPU로 자동 대체 (약 10배 느림) |
| Windows + PowerPoint (pptx) | 내장 이미지 OCR로 대체, `slide_render_unavailable` diagnostic |

세 경우 모두 변환은 성공하고 품질만 떨어진다. 전부 `retryable: true`로 표시되므로 호출자가
환경이 정상인 호스트에서 재시도할지 판단할 수 있다.
