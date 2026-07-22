from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from codegate_api.agent.gateway import AgentGatewayError, AgentRunResult, ClaudeAgentGateway
from codegate_api.config import Settings
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository
from codegate_api.knowledge.schemas import AccessLevel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals/agent_prompt_cases.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run read-only Claude Agent SDK prompt canaries against the demo corpus.",
    )
    parser.add_argument("--model", help="Exact Claude model ID; required for live evaluation.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Case ID to run.")
    parser.add_argument(
        "--list", action="store_true", help="List validated case IDs without API use."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    return parser.parse_args()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("evaluation corpus must be a non-empty JSON array")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("every evaluation case must be an object")
        case_id = raw.get("id")
        expected = raw.get("expected")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("evaluation case IDs must be unique non-empty strings")
        if not isinstance(raw.get("message"), str) or not isinstance(expected, dict):
            raise ValueError(f"case {case_id} requires message and expected objects")
        seen.add(case_id)
        cases.append(raw)
    return cases


def _is_subsequence(required: list[str], actual: list[str]) -> bool:
    positions = iter(actual)
    return all(
        any(candidate == required_name for candidate in positions) for required_name in required
    )


def _grade(case: dict[str, Any], result: AgentRunResult) -> list[str]:
    decision = result.decision
    expected = case["expected"]
    failures: list[str] = []
    for field in ("outcome", "intent", "target_document_id", "source_sha256"):
        if field not in expected:
            continue
        actual = getattr(decision, field)
        actual_value = actual.value if hasattr(actual, "value") else actual
        if actual_value != expected[field]:
            failures.append(f"{field}: expected {expected[field]!r}, got {actual_value!r}")

    expected_operation = expected.get("operation")
    if expected_operation is None and decision.operation is not None:
        failures.append("operation: expected null")
    elif isinstance(expected_operation, dict):
        if decision.operation is None:
            failures.append("operation: expected replace_exact, got null")
        else:
            for field in ("expected_text", "replacement_text"):
                if getattr(decision.operation, field) != expected_operation[field]:
                    failures.append(f"operation.{field}: mismatch")

    for term in expected.get("forbidden_query_terms", []):
        if term.casefold() in decision.search_query.casefold():
            failures.append(f"search_query contains forbidden control term {term!r}")
    required_tools = expected.get("required_tool_sequence", [])
    if not _is_subsequence(required_tools, result.tool_calls):
        failures.append(
            f"tool sequence: required {required_tools!r}, observed {result.tool_calls!r}"
        )
    forbidden_tools = set(result.tool_calls) - {
        "mcp__codegate__knowledge_search",
        "mcp__codegate__document_get",
        "mcp__codegate__knowledge_document_read",
        "mcp__codegate__source_file_read",
    }
    if forbidden_tools:
        failures.append(f"forbidden tool calls: {sorted(forbidden_tools)!r}")
    return failures


async def _run_case(
    case: dict[str, Any],
    *,
    model: str,
    state_root: Path,
) -> tuple[str, list[str]]:
    source_root = PROJECT_ROOT / "demo/source"
    repository = KnowledgeRepository(
        PROJECT_ROOT / "demo/llm-wiki",
        SourceUriResolver(source_root),
    )
    repository.load()
    gateway = ClaudeAgentGateway(
        Settings(
            agent_mode="claude",
            claude_model=model,
            source_root=source_root,
            agent_state_root=state_root,
        )
    )
    context = AccessContext(
        subject_id="prompt-evaluator",
        tenant_id="demo",
        readable_access=frozenset({AccessLevel.PUBLIC}),
        writable_document_ids=frozenset({"REG-000001", "REG-000002", "MAN-000001", "MAN-000002"}),
        provisioned=True,
        authz_source="prompt-eval",
    )
    try:
        result = await gateway.decide(
            message=case["message"],
            selected_document_id=case.get("selected_document_id"),
            conversation_id=f"eval-{case['id']}",
            repository=repository,
            access_context=context,
        )
    except AgentGatewayError as error:
        return str(case["id"]), [f"gateway rejected the result: {error.code}"]
    return str(case["id"]), _grade(case, result)


async def _run(cases: list[dict[str, Any]], *, model: str) -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="codegate-prompt-eval-") as state_directory:
        state_root = Path(state_directory)
        for case in cases:
            case_id, reasons = await _run_case(case, model=model, state_root=state_root)
            if reasons:
                failures += 1
                print(f"FAIL {case_id}: {'; '.join(reasons)}")
            else:
                print(f"PASS {case_id}")
    print(f"result: {len(cases) - failures}/{len(cases)} passed; model={model}")
    return 1 if failures else 0


def main() -> int:
    args = _parse_args()
    cases = _load_cases(args.cases.resolve())
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case["id"] in requested]
        missing = requested - {str(case["id"]) for case in cases}
        if missing:
            raise ValueError(f"unknown case IDs: {', '.join(sorted(missing))}")
    if args.list:
        for case in cases:
            print(f"{case['id']}\t{case['category']}")
        return 0
    if not args.model:
        raise SystemExit("--model is required for a reproducible live evaluation")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required for live evaluation")
    return asyncio.run(_run(cases, model=args.model))


if __name__ == "__main__":
    raise SystemExit(main())
