# doc2md

문서(pdf/docx/xlsx/pptx/csv/html/hwp/hwpx)를 프론트매터가 포함된 마크다운으로 변환하는
순수 네이티브 Python FastAPI 서비스. 외부 바이너리/subprocess 없이 pip 패키지만으로 동작한다.

**v0.2.0** — `POST /convert`는 v0.1.0 응답 모양으로 동결됐고, 새 기능은 전부
`POST /v2/convert`에 있다. 이유는 [API 버전](#api-버전) 참조.

설치는 [requirement.md](requirement.md) — 패키지, GPU 설정, 자동 다운로드되는 OCR 모델(최대 3.1GB).

## 파이프라인

```
파일 → [변환] → [후처리 정제] → [메타데이터 추출] → [프론트매터 조립] → [청킹] → 마크다운
```

## 실측 성능

31개 문서(hwp 3 / hwpx 2 / pdf 25 / pptx 1) 전체 변환 — **228.5초, 31/31 성공** (GPU, 콜드 캐시).
결과는 368청크 · 844섹션 · 1,224,052자. 모든 줄이 500자 이하다.

| 파일 | 크기 | 시간 | 결과 |
|---|---:|---:|---|
| pdf (353페이지) | 3.3MB | 101.2초 | 92청크 |
| pptx | 118.2MB | 32.9초 | 이미지 106장 OCR |
| hwpx (대용량) | 3.7MB | 17.2초 | 210청크 · 778섹션 |
| pdf (이미지 21장) | 0.4MB | 13.2초 | 5청크 |
| 나머지 27개 | — | 0.0~12.4초 | 대부분 1초 미만 |

캐시가 살아 있으면 같은 코퍼스가 **2초**대에 끝난다.

시간은 대부분 두 곳에서 나온다. **353페이지 PDF의 101초**는 OCR이 아니라 markitdown의
텍스트 추출 자체이고(페이지 단위로 변환하는 대가), **pptx의 33초**는 내장 이미지 106장을
읽는 비용이다.

PDF 텍스트 추출을 PyMuPDF `get_text()`로 바꾸면 66.3초 → 1.4초(47배)가 되고 한글
문자수도 완전히 동일하지만, **표가 전부 사라진다** — 28K자 문서 하나에서 표 210행이
0행이 된다. 표 보존이 더 중요하다고 판단해 교체하지 않았다.
근거는 [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

### 지나온 길

pptx 한 건이 처음엔 **1,241초**였다. 임베드된 그림을 하나씩 OCR했는데 9슬라이드 덱에
그림이 109장이었기 때문이다. 그래서 **슬라이드를 통째로 한 장씩 렌더링**하는 방식으로
바꿔 19초까지 줄였다.

그런데 그게 더 큰 문제를 만들었다 — 렌더된 슬라이드를 OCR하면 markitdown이 이미 읽은
텍스트를 **다시, 더 나쁘게** 읽는다(아래 [OCR](#ocr) 참조). 지금은 다시 이미지 단위로
읽되, 승격 없이 `fast` 모드로만 돌리고 중복을 걸러낸다. 33초는 그 결과다.

## 라우팅 규칙 (변환 단계)

**텍스트 변환이 정본이고, OCR은 그림 안만 읽는다.**

| 형식 | 텍스트 | 이미지 위치 복원 |
|---|---|---|
| `.hwpx` | `pyhwp2md` | zip 내부 `section*.xml`의 `<hp:pic>` 앞 텍스트를 앵커로 |
| `.hwp` | `pyhwp2md` | ❌ 바이너리 CFB라 불가 → `hwp_images_unsupported` |
| `.pptx` | `markitdown` | markitdown이 남긴 `![](그림90.jpg)`를 그 자리에서 치환 |
| `.pdf` | `markitdown` 페이지 단위 | 페이지 번호 + y좌표 |
| 그 외 | `markitdown` | — |

예외 하나: **텍스트 레이어가 없는 스캔 PDF**는 페이지를 통째로 렌더링해 OCR한다.
보존할 텍스트도 표도 애초에 없으므로 이미지화로 잃는 게 없다.

왜 통짜 렌더링을 그만뒀는지는 [OCR](#ocr)에 적었다.

## OCR

### OCR은 그림 안만 읽는다

markitdown은 슬라이드 이미지를 `![alt](img)`로만 참조할 뿐 **이미지 안의 텍스트는 읽지
못한다**. 그 한 가지가 OCR이 필요한 이유이고, **그것만이 OCR이 할 일이다.**

한동안은 슬라이드/페이지를 통째로 렌더링해 OCR했다. 속도는 좋았지만 변환기가 이미 읽은
텍스트를 다시 읽었고, 그것도 더 나쁘게 읽었다:

| | 실측 |
|---|---|
| OCR 줄 중 markitdown 출력과 중복 | **80%** (331/414) |
| OCR이 본문에서 차지하는 비중 | **37%** |

markitdown이 붙여 놓은 이름·직책 쌍이

```
Chaejeong Heo        →   Chaejeong Heo / CEO / CTO   (연결 소실)
CEO · CTO
```

15자짜리 조각으로 흩어지고, 여러 도형으로 그린 표는 행 구조를 잃었다. 게다가 그 덱에는
**진짜 표 shape이 0개**였다 — 이미지로 만들어서 얻을 표 자체가 없었다.

지금은 텍스트 변환 결과가 정본이고, OCR은 각 형식에서 찾아낸 **그림에만** 돌린 뒤 그림이
있던 자리에 결과를 넣는다(위 [라우팅 규칙](#라우팅-규칙-변환-단계) 참조).
같은 코퍼스에서 **OCR 비중 37% → 1.8%**, 마크다운 표 8,051행 보존.

예외는 **텍스트 레이어가 없는 스캔 PDF** 하나다. 보존할 텍스트도 표도 없으므로 페이지를
통째로 렌더링(150 DPI)해 읽는다.

### 삽입할 가치가 있는 것만 넣는다

그림을 읽으면 쓸모없는 조각이 딸려 온다. 두 가지로 거른다.

- **본문에 이미 있는 줄은 버린다.** 그림은 옆에 있는 글자를 그대로 담고 있는 일이 잦다
  (배너에 박힌 제목, 텍스트 상자와 같은 캡션). 공백을 무시하고 비교한다 — OCR은 레이아웃이
  꺾이는 곳에서 줄을 바꾸지만 변환기는 그러지 않기 때문이다.
- **글자가 하나도 없는 줄은 버린다.** 실제로 2.3MB짜리 사진 하나가 `[':', '208']`로
  인식됐는데, 그 사진이 덱에서 10곳에 재사용돼 의미 없는 `208`이 **10번** 삽입됐다.
  문맥을 잃은 숫자는 인용할 수 있는 사실이 아니다. 문맥이 있는 숫자는 그대로 산다 —
  `10,000천원`, `P1 프로그램` 모두 보존되고, 이 규칙은 OCR 결과에만 적용되므로 문서
  자체 텍스트의 `37℃`나 `($600↑)`는 건드리지 않는다.

내장 이미지는 **`fast` 모드 고정**이고 `structure`로 승격하지 않는다. 승격은 이미지 한 장당
15~24초인데(106장짜리 덱에서 230초 vs 33초), 그 값어치는 *렌더된 페이지 전체*에서 나온다.
정작 지켜야 했던 표는 이제 텍스트 변환기가 만들어 주므로 승격할 이유가 없다.

### 속도와 정확도: fast / structure / auto

| 모드 | 슬라이드당 | init | 추출 문자수 |
|---|---:|---:|---:|
| `fast` (검출+인식) | **0.61초** | 2.3초 | **2,222자** |
| `structure` (레이아웃+표+인식) | 10.6초 | 29초 | 1,718자 |

측정해 보니 구조 파싱은 **17배 느린데 텍스트는 오히려 적게** 뽑았다(레이아웃 단계가 본문으로
분류하지 않은 영역을 버린다). 유일한 실질 이점은 **표 복원**이다.

그래서 기본값은 **`auto`**: 모든 이미지에 빠른 OCR을 돌린 뒤, 인식된 박스 좌표의 **기하 구조만
보고**(추가 추론 0) 표처럼 보이는 이미지에만 구조 파싱으로 승격한다. 임계값(행 3개 이상,
열 정렬 90% 이상)은 실제 슬라이드·표로 튜닝했다 — 슬라이드 4장 오탐 0건, 표 탐지 성공.
승격 비용이 ~10초라 오탐이 미탐보다 훨씬 비싸므로 **엄격한 쪽으로** 잡았다.

`DOC2MD_OCR_MODE=fast|structure|auto`로 강제할 수 있다.

### 모델과 디바이스

- 인식 모델은 **문서에서 이미 추출한 텍스트로 언어를 판별해** 자동 선택한다(ko/en/ja/zh/latin).
  이미지를 다시 probe하지 않으므로 언어 선택에 추가 추론 비용이 없다.
- ⚠️ PaddleOCR은 **모델명을 하나라도 지정하면 `lang` 인자를 무시한다.** 그래서 인식 모델을
  반드시 이름으로 명시한다 — 이걸 놓치면 조용히 중국어 모델로 한글을 읽어
  (`입찰안내서`→`吾卫出京`) 표가 통째로 사라진다. 실제로 겪은 버그다.
- 구조 파싱은 PaddleOCR 기본(큰) 레이아웃 모델을 그대로 쓴다. 경량 `PP-DocLayout-S`로 바꿨더니
  **표를 이미지로 오분류**해서 표 복원이 무력화됐다. 속도는 레이아웃 모델을 약화시켜서가 아니라
  *구조 파싱을 적게 돌려서* 얻어야 한다.
- **GPU를 자동으로 쓴다.** CUDA가 실제로 동작하는지 실연산으로 확인한 뒤 사용하고, 실패하면
  (VRAM 부족 등) CPU로 자동 폴백한다. `paddlepaddle-gpu`는 CUDA 런타임이 번들된
  PaddlePaddle 공식 인덱스에서 설치해야 한다 — PyPI 빌드는 CUDA Toolkit이 따로 있어야 하고,
  `bin`이 없는 헤더용 CUDA 설치본만 있으면 `cublas64_12.dll`을 못 찾아 실패한다.
- 표 출력은 HTML이라 **마크다운 파이프 표로 변환**한다. 단 `rowspan`/`colspan` 병합셀 표는
  마크다운으로 표현하면 데이터가 어긋나므로 **HTML 그대로 보존**한다.
- 아이콘(면적 20,000px² 미만)은 건너뛰고, 큰 이미지는 긴 변 1,200px로 줄여서 넣는다.
- **이미지는 절대 디스크에 남지 않는다.** 메모리에서 numpy로 디코딩해 바로 모델에 넘기고,
  모듈 밖으로 나가는 것은 인식된 텍스트/마크다운뿐이다. 원본 이미지 참조도 후처리에서 제거된다.
- paddle 미설치·CUDA 실패·모델 다운로드 실패 등 어떤 사유로든 OCR이 불가능해도 죽지 않고
  `warnings`에 사유를 담아 나머지 변환은 정상 진행한다.

## 후처리 정제 (`postprocess.py`)

원본을 손상시킬 수 없는 결정적 변환만 수행한다:

- **이미지 참조 제거**: 이미지를 저장하지 않으므로 `![](그림9.jpg)` 같은 참조는 생성 시점부터
  깨진 링크다. 이미지의 내용은 OCR 텍스트가 대신하므로 참조 자체는 삭제한다.
- **글리프 런 붕괴**: PDF 레이어드 글리프로 `실실실…실시시시…시`처럼 같은 글자가 10~15회 반복돼
  깨진 표지 텍스트를 원복(`실시설계 기술제안입찰 [BIM 포함사업]`). 글자 4회+/기호 6회+/공백 6칸+만
  대상이고 `- | =`(표·구분선)과 숫자는 건드리지 않는다.
- **빈 표 제거**: 데이터 셀이 전부 비어있는 레이아웃용 표를 삭제.
- **깨진 표 복구**: 병합셀이 있으면 변환기가 표를 망가뜨린다. 실제로 HWP 예산표 하나가
  **33열 19행에 채움 3%**, 헤더는 4칸인데 구분선은 20칸으로 나왔다. 마크다운 렌더러가
  깨지고, 표처럼 보이는데 내용은 거의 없으며, `|  |  |` 빈 구조물이 내용보다 토큰을 더 먹는다.

  열의 빈 여부는 **헤더가 아니라 데이터 행으로** 판정한다. 데이터가 하나도 없는 열의
  헤더 텍스트는 컬럼 라벨이 아니라 **변환기가 한 행으로 뭉쳐버린 본문 문단**이기 때문이다
  (`8. 성과활용방안`, `9. 소요예산` …). 그런 텍스트는 표 위로 빼내 문단으로 되살린다.

  **무손실이다.** 열은 그 안의 모든 데이터 셀이 비어있을 때만 제거되고, 헤더에 있던
  글자는 삭제하지 않고 이동시킨다. 코퍼스 전체 검증: 표 구조를 뺀 실제 글자수
  **815,425자 → 815,425자로 완전 동일**(순서만 변경). 정상적인 표는 그대로 둔다.

  결과: 깨진 표 **34개 → 2개**, 행마다 열 수가 다른 표 **0개**.
- **줄 길이 상한 500자**: 한 줄이 500자를 넘으면 **문장 경계에서** 줄을 나눈다.
  후속 근거 추출이 수백 자 단위로 인용하므로, 700자짜리 한 줄은 예산을 넘기거나
  임의 위치에서 잘린다. 여기서 미리 나누면 독자가 이미 쉬었을 지점에서 끊긴다.

  우선순위: 문장 끝(`.`/`다.`/`음.`/`임.`) → 절 경계(`,`/`·`/`하여`/`하고`) → 공백 →
  (전부 없으면) 강제 절단. 글자는 하나도 잃지 않는다.

  **상한은 표 행에도 무조건 적용된다.** 500자를 넘는 표 행은 나뉘고, 이어지는 줄은
  더 이상 표의 일부로 파싱되지 않는다. 의도한 맞교환이다 — 줄 길이 상한이 그 행이
  기계 판독 가능한 것보다 우선한다. 영향 범위는 코퍼스에서 **16행**이고, 상한 이하인
  표 행은 그대로라 위의 표 복구 결과는 유지된다.

  코드 블록만 예외다. 공백이 의미를 갖기 때문에 줄바꿈이 동작을 바꾼다.
- **제목/H1 승격**: H1이 없거나 중간 줄이 잘못 H1로 승격된 문서에서, **첫 실질 라인**(배너 `< … >` 또는
  표지 줄)을 문서 제목으로 잡아 H1으로 만든다. 단 **슬라이드 덱(.pptx/.ppt)은 파일명을 제목으로
  쓴다** — 슬라이드 1의 제목 placeholder는 덱 이름이 아니라 섹션 라벨/템플릿 잔재인 경우가 많다
  (실제로 한 덱은 제목이 `Value`로 잡혔다).

일부러 하지 않는 것(잘못 고치면 더 나빠지므로): 잘못 매겨진 헤딩 레벨 재조정, 추출 과정에서 한 셀로
합쳐진 표를 다시 행으로 분해.

## 청킹 (`chunker.py`)

입찰안내서 한 건이 700KB를 넘어 LLM 프롬프트에 통째로 들어가지 않는다. 그래서 본문을
모델에 넘길 수 있는 크기로 쪼개되, **각 조각이 의미적으로 온전하도록** 나눈다.

- **표와 코드는 절대 쪼개지 않는다.** 표가 반으로 잘리면 헤더 행과 데이터 행이 서로 다른
  프롬프트로 흩어져 오히려 원문보다 나빠진다. 구조 파싱으로 어렵게 살려낸 표를 여기서
  깨뜨릴 수는 없다. 표 하나가 `max_chars`를 넘으면 **그 청크만 커진다**(분할 대신 초과 허용).
- **헤딩 경계를 우선해 분할**하므로 청크가 임의의 윈도우가 아니라 하나의 섹션이 된다.
- **각 청크는 상위 헤딩 breadcrumb를 갖는다**(`<!-- section: 제1장 총칙 > 1-1 공사비 -->`).
  문서 중간에서 뽑혀 나온 청크도 자기가 어느 섹션 소속인지 말할 수 있어야 한다.
- **각 청크는 자체 프론트매터를 갖는다.** `id`는 문서 것을 그대로 물려받고(같은 문서이므로)
  `chunk_no`만 `{파일명}_001`, `_002` … 로 달라진다. 검색된 청크에서 원본 문서와 위치를
  항상 역추적할 수 있다.
- 크기 단위는 **토큰이 아니라 문자**다. 토크나이저는 추가 의존성이고 모델마다 다르다.
  한글은 1자 ≈ 0.5~1토큰이라 기본값 4000자면 일반적인 컨텍스트에 충분히 들어간다.

### 분할 전략

검색(RAG)용이면 `fixed` 기본값을 그대로 쓰면 된다. 청크가 커질수록 한 덩어리에 무관한 내용이
섞여 임베딩이 흐려지고 LLM에도 노이즈가 함께 들어가므로, 검색 품질은 작은 청크에서 나온다.
이 코퍼스는 **줄당 평균 45자**라 기본값 4,000자는 약 90줄 / 3천~5천 토큰으로,
RAG 통설인 500~1,500토큰대와 맞는다. (참고: 1만 줄은 약 45만 자 ≈ 31만 토큰으로 어떤 API
컨텍스트도 넘긴다. 이 코퍼스에서 가장 큰 문서 *전체*가 1.2만 줄이다.)

- **`fixed`** (기본, 권장): 모든 청크를 `max_chars`(기본 4,000자) 이하로. 검색/RAG용.
- **`ratio`**: 청크 크기를 **문서 길이의 1/parts**로 잡아 문서당 약 `parts`개가 나오게 한다.
  각 조각을 통째로 API에 넣어 요약·분석할 때만 쓸 것 — **검색용으로는 부적합**하다
  (710K자 문서면 청크 하나가 14만 자라, "입찰 참가자격"을 물어도 14만 자가 딸려온다).
  짧은 문서를 억지로 쪼개면 무의미한 조각이 되므로 `ratio_floor_chars`(2,000) 아래로는
  내려가지 않고, 한 청크가 컨텍스트를 넘지 않도록 `ratio_cap_chars`(120,000)로 상한을 둔다.

```json
POST /convert
{ "path": "...", "chunking": { "enabled": true, "strategy": "ratio", "parts": 5 } }
```

`ratio`, `parts=5` 실측:

| 본문 길이 | 청크 수 | 평균 | 비고 |
|---:|---:|---:|---|
| 710,140 | 8 | 88,765 | 1/5=142K가 상한 120K를 넘어 상한 적용 |
| 330,908 | 6 | 55,180 | |
| 39,014 | 6 | 6,545 | |
| 15,295 | 6 | 2,547 | |
| 1,020 | 1 | 1,020 | 하한 미달 → 분할 안 함 |

정확히 `parts`개가 아니라 **약 `parts`개**인 이유는 의미 경계가 우선하기 때문이다: 표를
쪼개지 않고, 헤딩을 본문과 분리하지 않으려면 목표 크기에 못 미쳐도 끊어야 할 때가 있다.
정확히 5개에 가깝게 하려면 `ratio_cap_chars`를 올리면 된다.

`fixed`(기본 4,000/2,400) 실측 — 같은 710,140자 문서: 217개 청크, 평균 3,270 / 중앙값 3,513,
1,000자 미만 1개, 고아 헤딩 0개.

## 진행률 · ETA (`progress.py`)

OCR이 붙은 덱 하나가 20분을 넘긴다. 블로킹 POST로는 그동안 사용자에게 보여줄 게 없고
프록시 타임아웃도 부른다. 그래서 변환을 **백그라운드 작업**으로 돌리고 진행률/ETA를 폴링한다.

**ETA는 하드코딩이 아니라 스스로 교정된다.** 진행도를 0~1 비율로 보고 실제 소요시간에서
외삽한다(`eta = elapsed / fraction - elapsed`). 기계가 느리거나 이미지가 유난히 무거워도
고정 상수로 거짓말하지 않고 추정치가 알아서 따라간다.

다만 hwp 변환처럼 **내부가 보이지 않는 단일 라이브러리 호출**은 끝날 때까지 진행도가 0이라
외삽할 게 없다. 그래서 포맷별 처리량(바이트/초)을
`$DOC2MD_DATA_ROOT/timing.json`에 학습해두고,
측정값이 생기기 전까지는 그 baseline으로 ETA를 낸다. `eta_source`가 어느 쪽인지 알려준다:

| `eta_source` | 의미 |
|---|---|
| `baseline` | 과거 실행 이력 기반 예측 (측정 전) |
| `measured` | 이번 실행의 실제 진행도에서 외삽 (자기교정) |
| `final` | 완료됨 (0s) |
| `unknown` | 해당 포맷 첫 실행이라 이력이 없음 → `null` |

즉 **어떤 포맷이든 최초 1회는 ETA가 `null`**이고, 그 실행이 이력이 되어 2회차부터 첫 순간에
바로 ETA가 나온다. 실측: 2회차에서 hwpx 15초/pdf 58초로 예측 → 실제 15초/58초.

파일당 진행률이 나오므로 여러 파일을 한 번에 넣고 각각의 ETA를 보여줄 수 있다. 작업은
**한 번에 하나씩** 처리한다(OCR이 이미 CPU를 다 쓰므로 병렬로 돌리면 모든 ETA만 나빠진다).
그래서 배치 ETA = 실행 중 파일의 잔여시간 + 대기 중 파일들의 예측시간이다.

```
POST /convert/async   { "path": "...", ... }        -> 202 { job_id, poll }
POST /convert/batch   { "paths": [...], ... }       -> 202 { batch_id, jobs[], poll }
GET  /jobs/{job_id}          -> { status, phase, percent, current/total,
                                  elapsed_seconds, eta_seconds, eta_source, message }
GET  /jobs/{job_id}/result   -> ConvertResult (미완료면 409)
GET  /batches/{batch_id}     -> { percent, done_files/total_files, eta_seconds,
                                  files: [ 파일별 위 스냅샷 ] }
GET  /jobs                   -> 최근 작업 목록
```

블로킹 `POST /convert`를 그대로 쓰면서 진행률만 보고 싶으면 요청에 `job_id`를 직접 넣으면
된다(클라이언트가 UUID 생성 → 같은 id로 `/jobs/{id}` 폴링).

진행 로그는 콘솔에도 찍힌다:

```
doc2md.progress INFO [4b22489e] convert 45.0% ETA 62s — 이미지 OCR 9/19 (슬라이드 5)
```

## 프론트매터 스키마

필드 순서는 아래 그대로 고정된다(YAML 덤프 시 `sort_keys=False`).

| 필드 | 채우는 방식 |
|---|---|
| `schema_version` | 상수 `"1.0.0"` |
| `id` | `{doc_type 접두사}-{6자리}`. 파일 내용 sha256 기준으로 한 번 발급되면 고정 |
| `title` | override > 본문 첫 실질 라인(H1). **덱(.pptx/.ppt)은 파일명** |
| `doc_type` | override (기본 `document`) |
| `language` | 한글/라틴 문자 비율로 자동 감지 (`ko`/`en`/`und`) |
| `revision` | override (기본 `"1"`) |
| `status` | override (기본 `active`) |
| `chunk_no` | `{청킹파일명}_{청킹번호}`. 파일명은 슬러그화되고 번호는 3자리 0패딩 (`3. 입찰안내서(송파하남선 1공구)` → `3_입찰안내서_송파하남선_1공구_001`). 청킹 미사용 시엔 1/1이므로 `_001` |
| `official_number` | 조달청 공고코드/공고번호 자동 추출, 없으면 `null` |
| `authority_level` | override 전용 |
| `issuing_org` | `수요기관/발주기관 :` 자동 추출, 없으면 `null` |
| `issued_on` / `effective_from` / `effective_to` | override 전용 |
| `source.filename` / `source.sha256` | 자동 (원본 파일 기준) |
| `source.uri` | override, 기본 `source://{filename}` |
| `access` | override (기본 `internal`) |
| `tags` / `aliases` | override |

모든 필드는 요청의 `metadata`로 덮어쓸 수 있다: **override > 자동추출 > 휴리스틱 기본값**.

## 입찰 메타데이터 자동 추출 (`metadata_extract.py`)

보수적으로만 채운다(틀린 값은 빈 값보다 나쁘므로). 확신 신호가 있을 때만 채우고 나머지는 `null`:

- `official_number`: 파일명/본문의 조달청 공고코드(`R26DC00229304` 형태) 우선, 없으면 숫자가 실제로
  들어있는 `공고번호 : …` 필드.
- `issuing_org`: `수요기관/발주기관 : …` 필드에서 조직명(산문 문장은 배제).

추출값은 요청의 `metadata` override로 항상 덮어쓸 수 있다 (override > 자동추출 > 휴리스틱).

## UTF-8 기본값

Windows 콘솔은 기본 코드페이지가 cp949라, 아무 조치 없이 print/log를 하면 한글 파일명·본문이
`UnicodeEncodeError`로 죽거나 깨진 문자로 출력된다. 이 서비스는 플랫폼/로케일과 무관하게
항상 UTF-8로 동작하도록 다음을 강제한다:

- `app/main.py` 임포트 시 `sys.stdout`/`sys.stderr`를 `encoding="utf-8"`로 `reconfigure()`
- 모든 파일 I/O(`cache.py`, `frontmatter.py`)는 `encoding="utf-8"` 명시
- `agent_tool_example.py`도 동일하게 자체 stdout/stderr를 UTF-8로 강제하고, Agent SDK가 띄우는
  CLI 서브프로세스에도 `env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}`를 전달

## 실행

```bash
cd services/doc2md

uv sync                  # CPU (기본)
uv sync --extra gpu      # GPU (NVIDIA + CUDA). OCR이 약 10배 빨라진다

uv run uvicorn app.main:app --reload --port 8000
```

pip를 쓴다면:

```bash
pip install -e .                                    # CPU
pip install -e ".[gpu]" --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

**GPU는 설치만 하면 끝이다.** 서비스가 기동 시 CUDA가 실제로 동작하는지 확인해서 자동으로
쓰고, 없거나 실패하면 CPU로 폴백한다. 코드나 설정을 바꿀 필요가 없다. 로그로 확인할 수 있다:

```
doc2md.ocr INFO OCR using GPU: NVIDIA GeForce MX450
doc2md.ocr INFO OCR pipeline (lang=ko, mode=fast, device=gpu) ready
```

`DOC2MD_OCR_DEVICE=cpu`로 강제할 수도 있다.

> GPU 휠은 PyPI가 아니라 PaddlePaddle 공식 인덱스에 있다(CUDA 런타임이 번들돼 있음).
> PyPI의 `paddlepaddle-gpu`는 CUDA Toolkit이 따로 설치돼 있어야 하고, `bin` 없이 헤더만 있는
> CUDA 설치본이면 `cublas64_12.dll`을 못 찾아 실패한다. uv는 `[tool.uv.sources]` 설정 덕에
> 이 인덱스를 알아서 쓴다.

PPTX는 Windows나 PowerPoint 없이 markitdown으로 텍스트를 읽고 내장 이미지만 OCR한다. PDF
페이지 렌더링은 PyMuPDF를 사용한다.

## API 버전

`POST /convert`의 응답은 **소비자가 `extra="forbid"`로 검증한다.** 최상위에 키가 하나라도
늘면 — `null`로 직렬화되더라도 — 스키마 검증이 실패하고 변환 전체가 거부된다.
실제로 `chunks`를 추가했다가 연동이 조용히 끊긴 적이 있다.

그래서 v1은 **7키로 동결**했고, `tests/test_v1_contract.py`가 소비자의 pydantic 모델을
그대로 복제해 이를 지킨다. 새 필드는 전부 `/v2/convert`로 간다.

| | `/convert` (v0.1.0) | `/v2/convert` (v0.2.0) |
|---|---|---|
| 응답 키 | 7개 고정 | v1 + 7개 추가 |
| 입력 | 절대경로만 | 경로 / bytes / object-storage URI |
| 인증 | 없음 | `DOC2MD_API_TOKEN` 설정 시 Bearer |
| 청크 | ❌ (`/jobs/{id}/result` 이용) | ✅ |

## API

```
GET  /health          -> {"status": "ok", "version": "0.2.0"}
GET  /health?deep=1   -> 실제 변환 스모크 테스트 + OCR 디바이스 + 보안 설정 상태
                         (실패 시 503) — /health 200만으로는 경로 접근성이 증명되지 않는다
GET  /capabilities    -> 형식별 parse / ocr / page_mapping / write_back 표

POST /convert         -> v0.1.0 응답 (7키 고정, 아래 표 참조)
POST /v2/convert      -> v0.2.0 응답
POST /convert/async   -> 202, job_id 반환
POST /convert/batch   -> 202, 파일별 job + 배치 ETA
GET  /jobs/{id}       -> 진행률 · ETA
GET  /jobs/{id}/result-> 완료된 변환 결과 (v2 모양)
GET  /batches/{id}    -> 배치 전체 진행률
```

### `POST /v2/convert`

```jsonc
// 요청
{
  "source": { "kind": "path",  "path": "/vol/source/입찰안내서.pdf" },
  // 또는 { "kind": "bytes", "filename": "x.pdf", "content_base64": "..." }
  // 또는 { "kind": "uri",   "filename": "x.pdf", "uri": "s3://bucket/key" }
  "metadata": { },        // optional override (v1과 동일)
  "chunking": { "enabled": true },
  "budget_tokens": null,  // optional, 기본 $DOC2MD_TOKEN_BUDGET
  "job_id": null
}
```

```jsonc
// 응답 — v1 7키 + 아래 7개
{
  "markdown": "...", "frontmatter": {...}, "body": "...",
  "format": "pdf", "library_used": "markitdown+ocr",
  "warnings": [], "cached": false,

  "source_sha256":    "0e53241e...",   // 원본 파일의 해시
  "canonical_sha256": "2e1ab8b2...",   // body의 해시 (프론트매터 제외, LF 정규화)
  "converter": { "name": "doc2md", "version": "0.2.0",
                 "library": "markitdown+ocr", "ocr_device": "gpu" },
  "sections": [
    { "ordinal": 2, "anchor_hint": "sec-002", "stable_key": "h-e005c718",
      "level": 2, "heading": "제1장 총칙", "heading_path": ["입찰안내서", "제1장 총칙"],
      "char_start": 15, "char_end": 34, "source_page": 3 }
  ],
  "diagnostics": [
    { "code": "slide_render_unavailable", "message": "...",
      "severity": "warning", "retryable": true, "detail": null }
  ],
  "budget": { "est_tokens": 33, "budget_tokens": 27000000,
              "routing": "processed", "reason": null },
  "chunks": [ ... ]
}
```

**`source_sha256` vs `canonical_sha256`** — 서로 다른 버전 관리 대상이다. 원본이 그대로여도
변환기가 좋아지면 canonical은 바뀌고, canonical을 편집해도 원본은 그대로다. 둘을 분리해야
"원본이 바뀐 것"과 "우리 변환이 바뀐 것"을 구분할 수 있다.

**`ordinal` vs `stable_key`** — 중간에 절이 하나 추가되면 그 뒤의 `ordinal`은 전부 밀린다
(`sec-004`가 다음 빌드에서 `sec-005`). `stable_key`는 heading path의 해시라 **문서의 다른
곳이 바뀌어도 그대로**다. 리비전 간 같은 섹션을 매칭할 때는 `stable_key`를 쓴다.

**`retryable`** — 같은 입력이 나중에 성공할 수 있는지. OCR 엔진 부재나 PDF 렌더 실패는
`true`(정상 호스트에서 재시도할 값어치가 있음). 문서에 애초에 텍스트가 없는
경우는 `false`.

### 구조 보증

`/convert`, `/v2/convert` 모두 **정확히 하나의 H1 · 비어있지 않은 H2 하나 이상 · 레벨 점프
없음**을 보장한다. 변환기 원본은 이걸 지키지 않는다 — 710K자 hwpx 하나는 H1을 **771개**
뱉었다(원본에서 모든 조문이 제목 스타일이었다). `structure.py`가 잉여 H1을 H2로 강등하면서
그 하위 계층도 한 단계씩 밀어 상대 구조를 보존한다.

이건 결정론적이다. 같은 body는 언제나 같은 heading을 낸다 — 소비자의 `sec-*` 앵커가
안정적으로 유지되는 근거가 이것뿐이다.

제목만 있고 본문이 없어 H2를 만들 수 없는 문서는 조용히 넘기지 않고
`structure_incomplete` diagnostic으로 보고한다.

### 배치 CLI — processed / excepted 분류

```bash
python -m app.batch_cli "<입력 폴더>" output --budget-tokens 100000
```

```
output/
  processed/   test_2_hwpx_001.md ...
  excepted/    예산 초과 문서의 청크
  manifest.jsonl
```

예산을 넘겨도 **변환과 청킹은 전부 정상 수행**된다. 분류만 달라진다.

Office가 열린 문서 옆에 남기는 잠금 파일(`~$보고서.pptx`, `.~lock.보고서.pptx#`)은
입력에서 제외한다. 확장자는 같지만 수백 바이트짜리 관리용 파일이고, 앱이 잡고 있어
대개 읽히지도 않는다 — 실제로 PowerPoint를 열어둔 채 배치를 돌렸다가 1건이 실패했다.

파일명 접두사(`p_`/`n_`) 대신 폴더를 쓴 이유: `chunk_no`가
`frontmatter.slugify(path.name)` 기반이라 접두사를 붙이면 `test_1_pdf_001` →
`p_test_1_pdf_001`이 되어 백엔드가 검증하는 stable ID와 어긋나고, 재실행하면 `p_p_`가 된다.
폴더 이동은 파일명을 건드리지 않는다.

`manifest.jsonl`은 폴더가 못 알려주는 "왜"를 담는다:

```json
{"file":"test (2).hwpx","sha256":"...","canonical_sha256":"...",
 "routing":"excepted","reason":"token_budget_exceeded",
 "est_tokens":485231,"budget_tokens":100000,"chunks":216,"sections":778}
```

### 보안 · 배포

`/convert`는 서버 자신의 파일시스템에서 절대경로를 연다. 두 통제 없이 공개망에 노출하면
임의 파일 읽기가 된다.

| 환경변수 | 효과 |
|---|---|
| `DOC2MD_API_TOKEN` | 설정 시 `Authorization: Bearer` 필수 |
| `DOC2MD_ALLOWED_ROOTS` | `kind:"path"`를 이 루트 하위로 제한 (심링크·`..` 해석 후 검사) |
| `DOC2MD_TOKEN_BUDGET` | 예산 기본값 (기본 27,000,000) |
| `DOC2MD_DATA_ROOT` | raw cache, stable ID, ETA 기록을 저장할 쓰기 가능한 런타임 디렉터리 |

둘 다 opt-in이다 — 현재 지원 배포는 백엔드와 같은 컨테이너에 loopback으로 두는 것이고,
거기서는 아무 역할이 없다. 별도 서비스 배포를 **가능하게** 만들려고 존재한다.

절대경로 입력만으로는 다른 호스트에서 동작하지 않는다(같은 filesystem namespace가 필수).
별도 서비스로 띄우려면 `kind:"bytes"` 또는 `kind:"uri"`를 쓰면 된다.
URI는 `pip install -e ".[uri]"`가 필요하고, `http(s)`는 **의도적으로 제외**했다 —
사설망에서 임의 URL을 가져오는 서비스는 SSRF 통로가 된다.

파일 경로 기반 캐시: 원문 변환 결과는 실제 파일 내용 SHA-256 기준으로
`$DOC2MD_DATA_ROOT/raw/`에 캐시된다. mtime과 크기가 같아도 내용이 달라지면 반드시 cache miss다.
`frontmatter`는 요청마다 `metadata` override를 반영해 매번 새로 조립되지만,
`frontmatter.id`는 파일 내용 기준으로 한 번 발급되면
`$DOC2MD_DATA_ROOT/id_by_sha256.json`에 고정되어 override 유무와 무관하게 같은 문서는 항상
같은 id를 갖는다. 상태 파일은 설치된 Python 패키지 옆에 쓰지 않고 원자적으로 교체한다.

## 테스트

```bash
pip install -e ".[test]"
pytest tests/ -q          # 106개
```

`tests/test_v1_contract.py`가 가장 중요하다 — 소비자의 pydantic 모델을 복제해 v1 응답이
7키를 유지하는지 검사한다. 여기가 깨지면 필드를 되돌리거나 `/v2/convert`로 옮겨야 한다.
모델을 느슨하게 고치는 건 답이 아니다.

## Agent 툴로 노출할 때

[claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)의
인프로세스 MCP 툴(`@tool` + `create_sdk_mcp_server`)로 감싸는 예제는
[`agent_tool_example.py`](agent_tool_example.py) 참고. 핵심만 발췌:

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
import httpx

@tool(
    "convert_to_markdown",
    "그래프에서 찾은 문서 파일을 프론트매터 포함 마크다운으로 변환한다",
    {"path": str},
)
async def convert_to_markdown(args: dict) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post("http://localhost:8000/convert", json={"path": args["path"]})
        resp.raise_for_status()
    return {"content": [{"type": "text", "text": resp.json()["markdown"]}]}

doc2md_server = create_sdk_mcp_server(name="doc2md", tools=[convert_to_markdown])

options = ClaudeAgentOptions(
    mcp_servers={"doc2md": doc2md_server},
    allowed_tools=["mcp__doc2md__convert_to_markdown"],  # server=doc2md, tool=convert_to_markdown
    env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
)
```

실행:

```bash
uv sync --extra agent
# 별도 터미널에서 uvicorn을 띄운 채로:
uv run python agent_tool_example.py "C:/path/to/문서.hwpx"
```
