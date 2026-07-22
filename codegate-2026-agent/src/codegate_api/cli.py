from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

import uvicorn

from codegate_api.config import Settings


def _default_data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/CODEGATE"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "CODEGATE"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "codegate"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codegate-local",
        description="Run the CODEGATE agent and LLM wiki against a user-approved local workspace.",
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--llmwiki-project-root", type=Path, required=True)
    parser.add_argument("--llmwiki-input-root", type=Path, required=True)
    parser.add_argument("--llmwiki-storage-root", type=Path, required=True)
    parser.add_argument("--tenant-id", default="local")
    parser.add_argument("--wiki-id", default="workspace")
    parser.add_argument("--data-root", type=Path, default=_default_data_root())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--cors-origin",
        action="append",
        dest="cors_origins",
        default=None,
        help="Allowed local frontend origin. May be repeated.",
    )
    parser.add_argument("--doc2md-url")
    parser.add_argument(
        "--agent-mode",
        choices=("gemini", "claude", "deterministic"),
        default="gemini",
        help="Model gateway. Gemini uses GEMINI_API_KEY and is the local product default.",
    )
    parser.add_argument(
        "--deterministic-agent",
        action="store_true",
        help="Deprecated alias for --agent-mode deterministic; retained for local development.",
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    source_root = args.source_root.expanduser().resolve()
    project_root = args.llmwiki_project_root.expanduser().resolve()
    input_root = args.llmwiki_input_root.expanduser().resolve()
    storage_root = args.llmwiki_storage_root.expanduser().resolve()
    for label, path in (
        ("source root", source_root),
        ("LLMWIKI project root", project_root),
        ("LLMWIKI input root", input_root),
    ):
        if not path.is_dir():
            raise SystemExit(f"{label} is missing or is not a directory: {path}")
    data_root = args.data_root.expanduser().resolve()
    cors_origins = args.cors_origins or [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    settings = Settings(
        environment="local",
        auth_mode="local",
        agent_mode="deterministic" if args.deterministic_agent else args.agent_mode,
        bootstrap_demo=False,
        cors_origins=cors_origins,
        knowledge_mode="llmwiki",
        source_root=source_root,
        runtime_root=data_root,
        database_path=data_root / "codegate.sqlite3",
        backup_root=data_root / "backups",
        agent_state_root=data_root / "claude",
        knowledge_releases_root=data_root / "llmwiki-compat",
        knowledge_pointer_path=data_root / "unused-codegate-pointer",
        llmwiki_project_root=project_root,
        llmwiki_input_root=input_root,
        llmwiki_storage_root=storage_root,
        llmwiki_tenant_id=args.tenant_id,
        llmwiki_wiki_id=args.wiki_id,
        doc2md_base_url=args.doc2md_url,
    )
    storage_root.mkdir(parents=True, exist_ok=True)
    return settings


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise SystemExit("codegate-local only binds to a loopback IP address")
    except ValueError as error:
        raise SystemExit("--host must be a loopback IP address such as 127.0.0.1 or ::1") from error
    # Import lazily so invoking the CLI does not construct the module-level ASGI app from an
    # unrelated working-directory .env before the explicit local settings have been validated.
    from codegate_api.main import create_app

    uvicorn.run(create_app(_settings(args)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
