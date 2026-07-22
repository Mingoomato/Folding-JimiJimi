# Backend repository guidance

Every coding session must begin by reading this file completely before inspecting or changing the
repository. Then read the relevant contract under `docs/` before working across a team boundary.

This repository is owned by Seongju and contains only the FastAPI, Claude Agent SDK runtime, knowledge-package adapter, approval workflow, deterministic file editing, versioning, Undo, and converter/graph orchestration boundaries.

Do not implement or modify the Next.js frontend, document converter internals, or knowledge-graph generation here. Consume those components only through the versioned contracts in `docs/INTEGRATION_CONTRACT.md`.

Before integration, run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Never commit secrets or real company documents. Disable Claude's built-in write and shell tools; expose only application-owned in-process MCP tools through a deny-by-default permission policy and `PreToolUse` hook. File writes require an allowlisted source root, exact `plan_hash` approval, base-hash comparison, backup, atomic replacement, and Undo. HWP and PDF remain read-only until a verified round-trip writer exists.
