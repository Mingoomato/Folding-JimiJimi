from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    SessionStore,
)

from codegate_api.agent.instructions import build_system_prompt
from codegate_api.agent.tools import DocumentReadToolService, create_codegate_tool_server
from codegate_api.knowledge.access import AccessContext
from codegate_api.knowledge.repository import KnowledgeRepository

SAFE_AGENT_TOOLS = frozenset(
    {
        "mcp__codegate__knowledge_search",
        "mcp__codegate__document_get",
        "mcp__codegate__knowledge_document_read",
        "mcp__codegate__source_file_read",
        "mcp__codegate__document_capabilities_get",
        "mcp__codegate__source_structure_read",
    }
)
SDK_CONTROL_TOOLS = frozenset({"StructuredOutput"})
ALLOWED_AGENT_TOOLS = SAFE_AGENT_TOOLS | SDK_CONTROL_TOOLS


async def enforce_tool_allowlist(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> HookJSONOutput:
    del tool_use_id, context
    tool_name = str(input_data.get("tool_name", ""))
    if tool_name in ALLOWED_AGENT_TOOLS:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Tool is outside the CODEGATE backend allowlist.",
        }
    }


def build_claude_agent_options(
    repository: KnowledgeRepository,
    *,
    working_directory: Path,
    state_directory: Path,
    access_context: AccessContext,
    output_schema: dict[str, object] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    session_store: SessionStore | None = None,
    document_reader: DocumentReadToolService | None = None,
) -> ClaudeAgentOptions:
    tool_server = create_codegate_tool_server(repository, access_context, document_reader)
    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(repository.agent_guide),
        tools=[],
        mcp_servers={"codegate": tool_server},
        strict_mcp_config=True,
        allowed_tools=sorted(ALLOWED_AGENT_TOOLS),
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"],
        permission_mode="dontAsk",
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[enforce_tool_allowlist]),
            ]
        },
        cwd=working_directory.resolve(),
        env={
            "CLAUDE_CONFIG_DIR": str(state_directory.resolve()),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        },
        setting_sources=[],
        skills=[],
        plugins=[],
        output_format=(
            {"type": "json_schema", "schema": output_schema} if output_schema is not None else None
        ),
        model=model,
        session_id=session_id,
        resume=resume,
        session_store=session_store,
        max_turns=8,
        max_budget_usd=0.50,
        enable_file_checkpointing=False,
    )


def create_claude_agent_client(
    repository: KnowledgeRepository,
    *,
    working_directory: Path,
    state_directory: Path,
    access_context: AccessContext,
    output_schema: dict[str, object] | None = None,
    model: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    session_store: SessionStore | None = None,
    document_reader: DocumentReadToolService | None = None,
) -> ClaudeSDKClient:
    return ClaudeSDKClient(
        options=build_claude_agent_options(
            repository,
            working_directory=working_directory,
            state_directory=state_directory,
            access_context=access_context,
            output_schema=output_schema,
            model=model,
            session_id=session_id,
            resume=resume,
            session_store=session_store,
            document_reader=document_reader,
        )
    )
