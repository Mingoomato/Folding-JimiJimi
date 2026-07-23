# doc2md 설치 요구사항

이 서비스가 실제로 쓰는 것만 적었다. 측정 환경은 Windows 11 / Python 3.13.5 /
NVIDIA GeForce MX450이다.

## 요약

```bash
cd services/doc2md

# GPU 필수 — PaddlePaddle 전용 인덱스가 필요하다
pip install -e . --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/

# 개발/테스트
pip install -e ".[test]"

uvicorn app.main:app --host 127.0.0.1 --port 8931
curl 'http://127.0.0.1:8931/health?deep=1'   # 실제 변환까지 확인
```

`uv`를 쓰면 인덱스가 `pyproject.toml`에 선언돼 있어 `uv sync`로 끝난다.

## 1. 런타임

| | 요구 | 비고 |
|---|---|---|
| Python | **3.11 이상** | 검증 환경 3.13.5 |
| OS | Windows / Linux / macOS | OS별 필수 외부 프로그램 없음 |
| 디스크 | 패키지 약 3GB + **모델 최대 3.1GB** | 아래 §4 참조 |
| 메모리 | 4GB 이상 | 353페이지 PDF 변환 기준 |

**외부 바이너리·subprocess를 쓰지 않는다.** LibreOffice, 한글, PowerPoint, Tesseract 모두
필요 없다. pip 패키지만으로 동작한다.

> **PowerPoint는 더 이상 필요 없다.** 예전에는 슬라이드를 통째로 렌더링했지만, 현재는
> markitdown으로 텍스트를 읽고 내장 이미지만 직접 OCR한다.

## 2. Python 패키지

`pip install -e .`이 전부 가져온다. 각각이 왜 필요한지:

| 패키지 | 버전(검증) | 쓰임 |
|---|---|---|
| `fastapi` | 0.139.2 | HTTP API |
| `uvicorn[standard]` | 0.51.0 | ASGI 서버 |
| `pydantic` | 2.13.4 | 요청/응답 스키마, 계약 검증 |
| `python-multipart` | — | `kind:"bytes"` 업로드 |
| `markitdown[pdf,pptx,docx,xlsx]` | 0.1.6 | pdf·pptx·docx·xlsx·csv·html 텍스트 추출 |
| `python-pptx` | 1.0.2 | pptx 내장 이미지와 도형 이름 |
| `pyhwp2md` | 0.1.3 | hwp·hwpx 본문 (순수 python wheel) |
| `pymupdf` | 1.28.0 | PDF 페이지 분할, 페이지 렌더, 내장 이미지 추출 |
| `paddleocr` | 3.3.0 | OCR 파이프라인 |
| `paddlex[ocr]` | 3.3.13 | 레이아웃·표 구조 모델. **없으면 구성 시점에 DependencyError** |
| `paddlepaddle-gpu` | 3.2 이상 | CUDA 추론 엔진 |
| `nvidia-cudnn-cu12` | 9.x | Windows cuDNN·cuBLAS DLL |
| `pillow` / `numpy` | 12.3.0 / 2.5.1 | 이미지 디코딩 (메모리 내, 디스크 기록 없음) |
| `pyyaml` | 6.0.2 | 프론트매터 |

추가 extras:

| extra | 패키지 | 언제 |
|---|---|---|
| `gpu` | 없음 | 이전 설치 명령 호환용 빈 extra |
| `uri` | `fsspec`, `s3fs` | `/v2/convert`에 `kind:"uri"`(s3/gs/azure)로 넣을 때만 |
| `test` | `pytest` | 테스트 실행 |
| `agent` | `claude-agent-sdk`, `httpx` | Agent 툴 예제 |

## 3. GPU

서비스가 CUDA를 실연산으로 확인하고 사용 가능한 NVIDIA GPU를 순서대로 찾는다. GPU를
사용할 수 없으면 OCR을 중단하며 CPU로 자동 폴백하지 않는다.

```bash
pip install -e . --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

`paddlepaddle-gpu`는 PaddlePaddle 공식 인덱스에서 설치한다. Windows에서 필요한
`cudnn64_9.dll`과 cuBLAS DLL은 `nvidia-cudnn-cu12` 및 전이 의존성이 제공하며, doc2md가
해당 `bin` 디렉터리를 프로세스 DLL 검색 경로에 등록한다.

검증 환경:

```
paddle 3.2.0 | CUDA compiled: True
CUDA runtime: 12.6 | cuDNN: 9.9.0
GPU: NVIDIA GeForce MX450
```

- **NVIDIA + CUDA만 된다.** Intel/AMD 내장 GPU는 이 빌드에서 쓸 수 없다.
- cuDNN 버전이 wheel과 다르면 경고가 뜨지만(예: 빌드 9.9 vs 설치 9.5) 동작에는 문제없었다.
- `DOC2MD_OCR_DEVICE=gpu:N`으로 특정 GPU를 지정할 수 있고 `cpu` 값은 거부된다.

## 4. OCR 모델 — 설치 대상이 아니라 **자동 다운로드**

모델은 pip으로 설치하지 않는다. PaddleX가 **처음 필요할 때** 받아
`~/.paddlex/official_models/`에 캐시한다. 인터넷이 필요한 것은 이 최초 1회뿐이다.

### 항상 쓰이는 것 (약 27MB)

| 모델 | 크기 | 역할 |
|---|---:|---|
| `PP-OCRv5_mobile_det` | 4.8MB | 텍스트 검출 |
| `korean_PP-OCRv5_mobile_rec` | 14MB | 한국어 인식 |
| `en_PP-OCRv5_mobile_rec` | 7.7MB | 영어 인식 |

문서 언어에 따라 `japan_`·`latin_`·`PP-OCRv5_mobile_rec`(중국어)가 추가로 받아질 수 있다.

> ⚠️ PaddleOCR은 **모델명을 하나라도 지정하면 `lang` 인자를 무시한다.** 그래서 코드가
> 인식 모델을 반드시 이름으로 넘긴다. 이걸 놓치면 조용히 중국어 모델로 한글을 읽는다.

### 구조 파싱이 처음 호출될 때 (약 3.1GB)

**텍스트 레이어가 없는 스캔 PDF**에서 표처럼 보이는 레이아웃을 만나면 `PPStructureV3`가
구성되고, 그때 레이아웃·표 모델 전체가 받아진다.

| 모델 | 크기 |
|---|---:|
| `PP-Chart2Table` | **1.4GB** |
| `PP-FormulaNet_plus-L` | **702MB** |
| `SLANeXt_wired` | 349MB |
| `PP-DocLayout_plus-L` / `PP-DocBlockLayout` | 각 124~125MB |
| `RT-DETR-L_wired/wireless_table_cell_det` | 각 124MB |
| `PP-OCRv5_server_det` / `_server_rec` | 85MB / 82MB |
| `UVDoc` | 31MB |
| `SLANet_plus`, `PP-DocLayout-S`, `PP-LCNet_*` | 각 5~8MB |

> **`PP-Chart2Table`(1.4GB)과 `PP-FormulaNet_plus-L`(702MB)은 코드가
> `use_chart_recognition: False`, `use_formula_recognition: False`로 꺼 두었는데도
> 받아진다.** 그 플래그는 추론만 막고 모델 로딩은 막지 못한다. 즉 **2.1GB가 쓰지도 않을
> 모델**이다. 디스크가 빠듯하면 이 점을 감안해야 한다.

### 오프라인 설치

폐쇄망이면 인터넷이 되는 곳에서 한 번 변환해 `~/.paddlex/official_models/`를 통째로
복사하면 된다. 최소한 위 §"항상 쓰이는 것" 3개는 있어야 OCR이 동작한다.

`DISABLE_MODEL_SOURCE_CHECK=True`를 주면 기동 시 모델 호스트 접속 확인을 건너뛴다.

### OCR을 못 쓰는 상황

`paddleocr`·`paddlex[ocr]`·`paddlepaddle`은 **필수 의존성이라 설치가 생략되지 않는다.**
빼고 싶으면 설치 후 의도적으로 제거해야 한다.

다만 어떤 이유로든(패키지 제거, 모델 다운로드 실패, CUDA 오류) OCR이 불가능해도
**변환은 죽지 않는다.** 그림 안 텍스트만 빠지고 `ocr_engine_unavailable` diagnostic이
남는다(`retryable: true` — 정상 호스트에서 재시도할 값어치가 있다는 뜻).
본문 텍스트와 표는 전부 markitdown/pyhwp2md가 읽으므로 영향받지 않는다.

## 5. 환경변수

전부 선택 사항이다. 아무것도 설정하지 않아도 동작한다.

| 변수 | 기본값 | 효과 |
|---|---|---|
| `DOC2MD_OCR_DEVICE` | 자동 감지 | `cpu`/`gpu` 강제 |
| `DOC2MD_OCR_LANG` | `ko` | 기본 인식 언어 |
| `DOC2MD_OCR_MODE` | `auto` | `fast`/`structure`/`auto` |
| `DOC2MD_OCR_MIN_AREA` | `20000` | 이보다 작은 이미지는 건너뜀 (px²) |
| `DOC2MD_OCR_MAX_SIDE` | `1200` | 긴 변을 이 크기로 축소 |
| `DOC2MD_TOKEN_BUDGET` | `27000000` | 초과 문서를 `excepted`로 분류 |
| `DOC2MD_API_TOKEN` | 없음 | 설정 시 `Authorization: Bearer` 필수 |
| `DOC2MD_ALLOWED_ROOTS` | 없음 | `kind:"path"`를 이 루트 하위로 제한 |

## 6. 알려진 잔재

- **`pywin32`가 의존성에 남아 있지만 쓰이지 않는다.** 슬라이드 통짜 렌더링을 그만두면서
  `render.render_pptx_slides()`를 호출하는 곳이 사라졌다. Windows 외 환경에서는 애초에
  설치되지 않으므로(`sys_platform == 'win32'` 조건부) 해가 없지만, 정리 대상이다.
- 위의 **2.1GB 미사용 모델**도 같은 성격이다. PaddleX가 플래그와 무관하게 받는 것이라
  이쪽에서 막을 방법을 아직 찾지 못했다.

## 7. 설치 확인

```bash
# 1) 의존성과 계약
pytest tests/ -q                 # 106개 통과해야 정상

# 2) 실제 변환 + OCR 디바이스 + 보안 설정
curl 'http://127.0.0.1:8931/health?deep=1'
```

`/health?deep=1`은 합성 문서를 실제로 변환하고 아래를 함께 돌려준다. 실패하면 503이다.

```jsonc
{
  "version": "0.2.0",
  "ocr_available": true,
  "ocr_device": "gpu",          // "cpu"면 GPU가 안 잡힌 것
  "auth_required": false,       // DOC2MD_API_TOKEN 설정 여부
  "allowlist_enforced": false,  // DOC2MD_ALLOWED_ROOTS 설정 여부
  "allowed_root_count": 0,
  "convert": "ok",              // 합성 문서를 실제로 변환한 결과
  "temp_writable": true,
  "sections": 2,
  "status": "ok"
}
```

`GET /capabilities`로 형식별 지원 범위를 확인할 수 있다.

**주의**: 구조 파싱 모델을 처음 받는 실행은 3GB 다운로드 때문에 오래 걸린다. 그 뒤로는
캐시된다.
