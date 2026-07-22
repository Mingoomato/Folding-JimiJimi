import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import (
    __version__,
    budget,
    cache,
    capabilities,
    chunker,
    frontmatter,
    metadata_extract,
    ocr,
    postprocess,
    progress,
    security,
    sourcing,
    structure,
)
from app.converters import ConversionFailedError, convert
from app.errors import Doc2MdError, diagnostic
from app.schemas import (
    BatchConvertRequest,
    BudgetInfo,
    ConverterInfo,
    ConvertRequest,
    ConvertResultV1,
    ConvertResultV2,
    ConvertV2Request,
    SectionInfo,
)

# Windows defaults stdout/stderr to the system codepage (cp949 on ko-KR), which
# raises UnicodeEncodeError or silently mangles Korean filenames/content the
# moment anything gets printed or logged. Force UTF-8 unconditionally so the
# service behaves the same on every platform regardless of console/locale.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

app = FastAPI(title="doc2md", version=__version__)


@app.exception_handler(Doc2MdError)
def _doc2md_error(request: Request, exc: Doc2MdError) -> JSONResponse:
    """Structured 4xx/5xx bodies with an explicit `retryable` flag.

    Safe to apply to every endpoint including the frozen v1: the v0.1.0 consumer
    branches on the HTTP status only and never reads the body.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.body())


# Conversions are CPU-bound and serialized on purpose: OCR already saturates the
# machine, so running jobs concurrently would only make every ETA worse.
_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc2md")


@app.get("/health")
def health(deep: bool = False) -> dict:
    """Liveness, or with ``?deep=1`` an actual conversion.

    A plain 200 here proves the process is up, not that it can read the source
    volume or that OCR works — the deployment notes are explicit that a
    successful /health does not prove path accessibility. The deep variant
    converts a synthetic document end to end and reports what is actually
    configured, so a misconfigured deploy fails loudly instead of at first use.
    """
    if not deep:
        return {"status": "ok", "version": __version__}

    import tempfile

    checks: dict = {
        "version": __version__,
        "ocr_available": ocr.available(),
        "ocr_device": ocr.device(),
        **security.posture(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="doc2md_health_") as tmp:
            probe = Path(tmp) / "health.md"
            probe.write_text("# 상태 점검\n\n## 본문\n\n변환 확인용 문서.\n", encoding="utf-8")
            result = _run_pipeline(probe, ConvertRequest(path=str(probe)))
        checks["convert"] = "ok"
        checks["temp_writable"] = True
        checks["sections"] = len(result.sections)
        checks["status"] = "ok"
    except Exception as e:
        checks["convert"] = "failed"
        checks["error"] = str(e)
        checks["status"] = "degraded"
        return JSONResponse(status_code=503, content=checks)
    return checks


@app.get("/capabilities", response_model=capabilities.CapabilitiesResponse)
def get_capabilities() -> capabilities.CapabilitiesResponse:
    """Per-format parse vs original write-back support.

    ``write_back`` is false for every format and will stay false until a writer
    exists and is verified — treat this service as read-only.
    """
    return capabilities.CapabilitiesResponse(
        version=__version__,
        ocr_device=ocr.device(),
        ocr_available=ocr.available(),
        formats=capabilities.table(),
    )


def _run_pipeline(
    path: Path,
    req: ConvertRequest | ConvertV2Request,
    job=None,
    budget_tokens: int | None = None,
) -> ConvertResultV2:
    """The whole conversion, reporting progress into ``job`` when given."""
    raw = cache.get(path)
    was_cached = raw is not None
    if raw is None:
        raw = convert(path, job=job)
        cache.set(path, raw)
    else:
        progress.update(
            job, phase="convert", total=1, current=1, message="캐시된 변환 사용"
        )

    progress.update(job, phase="postprocess", total=1, current=0, message="본문 정제 중")
    cleaned = postprocess.clean(raw.body)
    progress.update(job, current=1)

    progress.update(job, phase="metadata", total=1, current=0, message="메타데이터 추출 중")
    auto_meta = metadata_extract.extract(cleaned.body, path.name)
    progress.update(job, current=1)

    progress.update(job, phase="frontmatter", total=1, current=0, message="프론트매터 조립 중")
    fm = frontmatter.build(
        path,
        cleaned.body,
        req.metadata,
        auto=auto_meta,
        title_hint=cleaned.title,
    )
    full_markdown = frontmatter.render(fm, cleaned.body)
    progress.update(job, current=1)

    chunks = None
    if req.chunking.enabled:
        progress.update(job, phase="chunking", total=1, current=0, message="청킹 중")
        chunks = chunker.build_document_chunks(path, fm, cleaned.body, req.chunking)
        progress.update(job, current=1, message=f"{len(chunks)}개 청크 생성")

    diagnostics = list(raw.diagnostics)
    diagnostics += structure.structural_diagnostics(cleaned.headings)

    est, budget_total, routing, reason = budget.decide(cleaned.body, budget_tokens)
    if reason:
        diagnostics.append(
            diagnostic(reason, detail=f"{est:,} tokens > {budget_total:,}")
        )

    return ConvertResultV2(
        chunks=chunks,
        markdown=full_markdown,
        frontmatter=fm,
        body=cleaned.body,
        format=raw.format,
        library_used=raw.library_used,
        warnings=[d.as_legacy_string() for d in diagnostics if d.severity != "info"],
        cached=was_cached,
        source_sha256=fm.source.sha256,
        canonical_sha256=frontmatter.canonical_sha256(cleaned.body),
        converter=ConverterInfo(
            version=__version__,
            library=raw.library_used,
            ocr_device=ocr.device() if ocr.available() else "unavailable",
        ),
        sections=[
            SectionInfo(**vars(sec)) for sec in structure.extract_sections(cleaned.body)
        ],
        diagnostics=diagnostics,
        budget=BudgetInfo(
            est_tokens=est,
            budget_tokens=budget_total,
            routing=routing,
            reason=reason,
        ),
    )


@app.post("/convert", response_model=ConvertResultV1)
def convert_document(req: ConvertRequest) -> ConvertResultV1:
    """Blocking conversion, v0.1.0 response shape. Frozen — see ConvertResultV1.

    Pass ``job_id`` to make progress pollable meanwhile. Chunks are deliberately
    absent from this response even when ``chunking.enabled`` is set: use
    ``POST /v2/convert``, or pass a ``job_id`` and read them back from
    ``GET /jobs/{job_id}/result``.
    """
    path = Path(req.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")

    job = progress.create(str(path), job_id=req.job_id) if req.job_id else None
    try:
        result = _run_pipeline(path, req, job)
    except ConversionFailedError as e:
        progress.fail(job, str(e))
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        progress.fail(job, str(e))
        raise
    progress.finish(job, result, cached=result.cached)
    return ConvertResultV1.of(result)


@app.post(
    "/v2/convert",
    response_model=ConvertResultV2,
    dependencies=[Depends(security.require_token)],
)
def convert_document_v2(req: ConvertV2Request) -> ConvertResultV2:
    """Conversion with the full v0.2 contract.

    Adds over v1: ``sections`` (stable keys, heading paths, char spans, page
    numbers where the format has them), ``canonical_sha256`` and
    ``source_sha256`` as separate artifacts, ``converter`` provenance,
    ``diagnostics`` with a retryable flag, ``budget`` routing, and ``chunks``.

    Accepts a source by path, inline bytes, or object-storage URI, so it does not
    require the caller to share a filesystem with this service.
    """
    with sourcing.materialize(req.source) as path:
        # Check before doing any work: a source that changed since the caller
        # read it must not be converted at all, let alone cached.
        if req.expected_source_sha256:
            actual = frontmatter.sha256_file(path)
            if actual != req.expected_source_sha256:
                raise Doc2MdError(
                    "SOURCE_HASH_MISMATCH",
                    "원본이 요청된 SHA-256과 일치하지 않아 stale 변환으로 거부했습니다.",
                    f"expected={req.expected_source_sha256} actual={actual}",
                )

        job = progress.create(str(path), job_id=req.job_id) if req.job_id else None
        try:
            result = _run_pipeline(
                path,
                req,
                job,
                budget_tokens=req.budget_tokens,
            )
        except ConversionFailedError as e:
            progress.fail(job, str(e))
            raise Doc2MdError("CONVERSION_FAILED", str(e)) from e
        except Doc2MdError:
            raise
        except Exception as e:
            progress.fail(job, str(e))
            raise Doc2MdError("INTERNAL_ERROR", "변환 중 오류가 발생했습니다.", str(e)) from e
        progress.finish(job, result, cached=result.cached)
        return result


@app.post("/convert/async", status_code=202)
def convert_document_async(req: ConvertRequest) -> dict:
    """Start a conversion in the background and return immediately.

    Long conversions (a deck with OCR runs into the minutes) do not fit a
    blocking request: poll ``GET /jobs/{job_id}`` for progress/ETA, then fetch
    ``GET /jobs/{job_id}/result`` once status is ``done``.
    """
    path = Path(req.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")

    job = progress.create(str(path), job_id=req.job_id)

    def _work() -> None:
        try:
            result = _run_pipeline(path, req, job)
        except Exception as e:
            progress.fail(job, str(e))
            return
        progress.finish(job, result, cached=result.cached)

    _workers.submit(_work)
    return {"job_id": job.job_id, "status": job.status, "poll": f"/jobs/{job.job_id}"}


@app.post("/convert/batch", status_code=202)
def convert_batch(req: BatchConvertRequest) -> dict:
    """Queue several files; each gets its own progress and ETA.

    Files convert one at a time (OCR already saturates the CPU), so the batch
    ETA is the running file's remaining time plus the predicted duration of the
    ones still queued.
    """
    missing = [p for p in req.paths if not Path(p).is_file()]
    if missing:
        raise HTTPException(status_code=404, detail=f"file(s) not found: {missing}")

    jobs = []
    for p in req.paths:
        job = progress.create(str(Path(p)))
        item = ConvertRequest(
            path=p,
            metadata=req.metadata,
            chunking=req.chunking,
            job_id=job.job_id,
        )

        def _work(item=item, job=job) -> None:
            try:
                result = _run_pipeline(Path(item.path), item, job)
            except Exception as e:
                progress.fail(job, str(e))
                return
            progress.finish(job, result, cached=result.cached)

        _workers.submit(_work)
        jobs.append({"path": p, "job_id": job.job_id})

    batch_id = progress.create_batch([j["job_id"] for j in jobs], req.batch_id)
    return {
        "batch_id": batch_id,
        "jobs": jobs,
        "poll": f"/batches/{batch_id}",
    }


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    snap = progress.batch_snapshot(batch_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
    return snap


@app.get("/jobs")
def list_jobs() -> dict:
    return {"jobs": progress.list_jobs()}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = progress.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job.snapshot()


@app.get("/jobs/{job_id}/result", response_model=ConvertResultV2)
def get_job_result(job_id: str) -> ConvertResultV2:
    job = progress.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    if job.status == "failed":
        raise HTTPException(status_code=422, detail=job.error or "conversion failed")
    if job.status != "done" or job.result is None:
        raise HTTPException(
            status_code=409, detail=f"job not finished (status={job.status})"
        )
    return job.result
