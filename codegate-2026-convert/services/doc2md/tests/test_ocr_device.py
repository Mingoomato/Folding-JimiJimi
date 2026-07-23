from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app import ocr


def _fake_paddle(*, cuda: bool = True, count: int = 1, unusable: set[int] | None = None):
    unusable = unusable or set()
    selected: list[str] = []

    def set_device(device: str) -> None:
        index = int(device.split(":", 1)[1])
        selected.append(device)
        if index in unusable:
            raise RuntimeError("GPU is busy")

    paddle = SimpleNamespace(
        device=SimpleNamespace(
            is_compiled_with_cuda=lambda: cuda,
            cuda=SimpleNamespace(
                device_count=lambda: count,
                get_device_name=lambda index: f"GPU {index}",
            ),
        ),
        set_device=set_device,
        zeros=lambda _shape: object(),
        nn=SimpleNamespace(functional=SimpleNamespace(conv2d=lambda _image, _kernel: object())),
    )
    return paddle, selected


@pytest.fixture(autouse=True)
def reset_ocr_state(monkeypatch):
    monkeypatch.delenv("DOC2MD_OCR_DEVICE", raising=False)
    monkeypatch.setattr(ocr, "_pipelines", {})
    monkeypatch.setattr(ocr, "_failed", False)
    monkeypatch.setattr(ocr, "_device_used", "unknown")
    monkeypatch.setattr(ocr, "_configure_nvidia_dll_directories", lambda: None)


def test_pick_device_uses_first_working_cuda_gpu(monkeypatch):
    paddle, selected = _fake_paddle(count=2, unusable={0})
    monkeypatch.setitem(sys.modules, "paddle", paddle)

    assert ocr._pick_device() == "gpu:1"
    assert selected == ["gpu:0", "gpu:1"]


def test_pick_device_rejects_cpu_only_paddle(monkeypatch):
    paddle, _ = _fake_paddle(cuda=False)
    monkeypatch.setitem(sys.modules, "paddle", paddle)

    with pytest.raises(ocr.OcrGpuUnavailableError, match="no CUDA support"):
        ocr._pick_device()


def test_pick_device_rejects_cpu_override(monkeypatch):
    monkeypatch.setenv("DOC2MD_OCR_DEVICE", "cpu")

    with pytest.raises(ocr.OcrGpuUnavailableError, match="CPU OCR is disabled"):
        ocr._pick_device()


def test_pipeline_failure_never_retries_on_cpu(monkeypatch):
    attempted: list[str] = []
    monkeypatch.setattr(ocr, "_pick_device", lambda: "gpu:0")

    def fail_build(_lang: str, device: str, _mode: str):
        attempted.append(device)
        raise RuntimeError("out of memory")

    monkeypatch.setattr(ocr, "_build", fail_build)

    assert ocr._get_pipeline() is None
    assert attempted == ["gpu:0"]
    assert ocr._device_used == "unavailable"
