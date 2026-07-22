from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from codegate_api.documents.writers.base import WriterError


class KordocWorkerClient:
    def __init__(
        self,
        *,
        node_bin: Path,
        kordoc_root: Path,
        staging_root: Path,
        timeout_seconds: float,
    ) -> None:
        self._node_bin = node_bin
        self._kordoc_root = kordoc_root
        self._staging_root = staging_root.resolve()
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is not None and process.poll() is None:
                self._kill_tree(process)

    def request(self, command: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            process = self._ensure_process()
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "command": command, **payload}
            assert process.stdin is not None
            try:
                process.stdin.write(
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._process = None
                raise WriterError(
                    "writer_crash", "Kordoc worker crashed", retryable=True
                ) from error
            try:
                response = self._responses.get(timeout=self._timeout_seconds)
            except queue.Empty as error:
                self._terminate(process)
                raise WriterError(
                    "writer_timeout", "Kordoc worker timed out", retryable=True
                ) from error
            if response.get("id") != request_id:
                self._terminate(process)
                raise WriterError(
                    "writer_protocol_error",
                    "Kordoc worker response correlation failed",
                    retryable=True,
                )
            if not response.get("ok"):
                raise WriterError("writer_failed", str(response.get("error", "Kordoc failed")))
            data = response.get("data")
            if not isinstance(data, dict):
                raise WriterError("writer_protocol_error", "Kordoc returned malformed output")
            return data

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self._staging_root)
        except ValueError as error:
            raise WriterError("unsafe_staging_path", "Kordoc path escapes staging root") from error
        return relative.as_posix()

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._staging_root.mkdir(parents=True, exist_ok=True)
        worker = Path(__file__).with_name("workers") / "kordoc_worker.mjs"
        creationflags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(
            [
                str(self._node_bin),
                str(worker),
                str(self._staging_root),
                str(self._kordoc_root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        self._process = process
        thread = threading.Thread(target=self._read_responses, args=(process,), daemon=True)
        thread.start()
        return process

    def _read_responses(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                response = {"id": None, "ok": False, "error": "malformed worker response"}
            if isinstance(response, dict):
                self._responses.put(response)

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        self._process = None
        if process.poll() is None:
            self._kill_tree(process)
        while not self._responses.empty():
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    @staticmethod
    def _kill_tree(process: subprocess.Popen[str]) -> None:
        if os.name == "nt" and (taskkill := shutil.which("taskkill")):
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
