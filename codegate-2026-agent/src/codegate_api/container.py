from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from codegate_api.agent.gateway import AgentGateway, build_agent_gateway
from codegate_api.application.chat_service import ChatService
from codegate_api.auth import TokenAuthenticator, build_authenticator
from codegate_api.changes.service import ChangeExecutionService, ChangePlanService
from codegate_api.config import Settings
from codegate_api.documents.capabilities import DocumentCapabilityRegistry
from codegate_api.documents.rendering import ManagedDocumentRenderer
from codegate_api.documents.service import DocumentService
from codegate_api.documents.store import DocumentStateStore
from codegate_api.documents.sync import DocumentSyncWorker
from codegate_api.documents.templates import DocumentTemplateRegistry
from codegate_api.documents.writers.registry import WriterRegistry
from codegate_api.files.atomic import DocumentLockManager, SafeSourceFileStore
from codegate_api.files.resolver import SourceUriResolver
from codegate_api.integrations.doc2md import Doc2MdClient
from codegate_api.knowledge.catalog import KnowledgeCatalog
from codegate_api.knowledge.llmwiki import (
    InProcessLLMWikiBuildRunner,
    LLMWikiCompatibilityAdapter,
    NativeLLMWikiCatalog,
    NativeLLMWikiPipeline,
)
from codegate_api.knowledge.pipeline import LocalKnowledgePipeline
from codegate_api.knowledge.worker import KnowledgeSyncWorker
from codegate_api.runtime_workspace import RuntimeWorkspace
from codegate_api.state.store import StateStore


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    workspace: RuntimeWorkspace
    state: StateStore
    resolver: SourceUriResolver
    files: SafeSourceFileStore
    document_files: SafeSourceFileStore
    catalog: KnowledgeCatalog | NativeLLMWikiCatalog
    pipeline: LocalKnowledgePipeline | NativeLLMWikiPipeline
    sync_worker: KnowledgeSyncWorker
    document_sync_worker: DocumentSyncWorker
    document_service: DocumentService
    plans: ChangePlanService
    executions: ChangeExecutionService
    agent: AgentGateway
    chat: ChatService
    authenticator: TokenAuthenticator
    doc2md: Doc2MdClient | None
    reset_lock: asyncio.Lock

    async def startup(self) -> None:
        if (
            self.settings.agent_mode == "gemini"
            and not os.environ.get("GEMINI_API_KEY", "").strip()
        ):
            raise RuntimeError("Gemini REST gateway requires GEMINI_API_KEY")
        if (
            self.settings.environment in {"local", "production"}
            and self.settings.agent_mode == "claude"
            and not os.environ.get("ANTHROPIC_API_KEY")
        ):
            raise RuntimeError("Claude Agent SDK requires ANTHROPIC_API_KEY")
        self.workspace.bootstrap()
        self.state.initialize()
        await self.executions.recover_files()
        has_pending_events = self.state.has_unpublished_events()
        self.catalog.bootstrap(
            allow_stale_sources=has_pending_events,
            replace_invalid_demo_seed=(
                self.settings.bootstrap_demo
                and self.settings.environment != "production"
                and not has_pending_events
            ),
            allow_seed_bootstrap=self.settings.bootstrap_demo,
        )
        self.state.record_knowledge_version(
            version=self.catalog.snapshot().version,
            parent_version=None,
            event_ids=[],
            status="active",
        )
        await self.document_service.prune_expired_managed_files()
        await self.document_service.recover_prepared_executions()
        self.sync_worker.start()
        self.document_sync_worker.start()

    async def shutdown(self) -> None:
        await self.document_service.shutdown()
        await self.document_sync_worker.stop()
        await self.sync_worker.stop()

    async def reset_demo(self) -> str:
        if self.settings.environment == "production":
            raise RuntimeError("demo reset is forbidden in production")
        async with self.reset_lock:
            self.state.reset()
            self.workspace.reset_source()
            version = self.catalog.reset_to_seed()
            self.state.record_knowledge_version(
                version=version,
                parent_version=None,
                event_ids=[],
                status="active",
            )
            return version

    @property
    def agent_available(self) -> bool:
        if self.settings.agent_mode == "deterministic":
            return True
        if self.settings.agent_mode == "gemini":
            return bool(os.environ.get("GEMINI_API_KEY", "").strip())
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    @property
    def converter_available(self) -> bool:
        return self.doc2md.health() if self.doc2md is not None else True


def build_container(settings: Settings) -> AppContainer:
    workspace = RuntimeWorkspace(settings)
    resolver = SourceUriResolver(settings.resolved_source_root())
    state = StateStore(settings.resolved_database_path())
    files = SafeSourceFileStore(
        resolver,
        backup_root=settings.resolved_backup_root(),
        max_bytes=settings.max_edit_bytes,
    )
    document_files = SafeSourceFileStore(
        resolver,
        backup_root=settings.resolved_backup_root(),
        recovery_root=settings.resolved_document_recovery_root(),
        max_bytes=settings.document_max_result_bytes,
    )
    locks = DocumentLockManager(settings.resolved_runtime_root() / "locks")
    doc2md = None
    if settings.doc2md_base_url:
        doc2md = Doc2MdClient(
            base_url=settings.doc2md_base_url,
            resolver=resolver,
            timeout_seconds=settings.doc2md_timeout_seconds,
            source_kind=settings.doc2md_source_kind,
            api_token=(
                settings.doc2md_api_token.get_secret_value()
                if settings.doc2md_api_token is not None
                else None
            ),
            max_source_bytes=settings.doc2md_max_source_bytes,
        )
    if settings.knowledge_mode == "llmwiki":
        project_root = settings.resolved_llmwiki_project_root()
        input_root = settings.resolved_llmwiki_input_root()
        storage_root = settings.resolved_llmwiki_storage_root()
        assert project_root is not None and input_root is not None and storage_root is not None
        runner = InProcessLLMWikiBuildRunner(
            project_root=project_root,
            input_root=input_root,
            storage_root=storage_root,
            tenant_id=settings.llmwiki_tenant_id,
            wiki_id=settings.llmwiki_wiki_id,
            actor_id=settings.llmwiki_actor_id,
        )
        adapter = LLMWikiCompatibilityAdapter(
            source_resolver=resolver,
            input_root=input_root,
            storage_root=storage_root,
            compatibility_root=settings.resolved_knowledge_releases_root(),
            tenant_id=settings.llmwiki_tenant_id,
            wiki_id=settings.llmwiki_wiki_id,
        )
        native_catalog = NativeLLMWikiCatalog(
            adapter=adapter,
            runner=runner,
            source_resolver=resolver,
            state_store=state,
        )
        catalog: KnowledgeCatalog | NativeLLMWikiCatalog = native_catalog
        pipeline: LocalKnowledgePipeline | NativeLLMWikiPipeline = NativeLLMWikiPipeline(
            catalog=native_catalog,
            file_store=files,
            state_store=state,
            lock_manager=locks,
            doc2md=doc2md,
        )
    else:
        catalog = KnowledgeCatalog(
            seed_root=settings.resolved_seed_knowledge_root(),
            releases_root=settings.resolved_knowledge_releases_root(),
            pointer_path=settings.resolved_knowledge_pointer_path(),
            source_resolver=resolver,
        )
        pipeline = LocalKnowledgePipeline(
            catalog=catalog,
            file_store=files,
            state_store=state,
            doc2md=doc2md,
        )
    sync_worker = KnowledgeSyncWorker(pipeline=pipeline, state_store=state)
    document_state = DocumentStateStore(settings.resolved_database_path())
    document_capabilities = DocumentCapabilityRegistry(settings)
    document_renderer = ManagedDocumentRenderer(
        libreoffice_bin=document_capabilities.libreoffice_bin,
        timeout_seconds=settings.document_worker_timeout_seconds,
    )
    document_writers = WriterRegistry(
        settings=settings,
        capabilities=document_capabilities,
        renderer=document_renderer,
    )
    document_templates = DocumentTemplateRegistry(
        settings.resolved_document_template_root(),
        max_bytes=settings.document_max_source_bytes,
    )
    document_sync_worker = DocumentSyncWorker(
        pipeline=pipeline,
        state=document_state,
    )
    document_service = DocumentService(
        plan_ttl_seconds=settings.plan_ttl_seconds,
        retention_days=settings.document_retention_days,
        artifact_root=settings.resolved_document_artifact_root(),
        max_preview_bytes=settings.document_max_preview_bytes,
        resolver=resolver,
        files=document_files,
        locks=locks,
        catalog=catalog,
        state=document_state,
        capabilities=document_capabilities,
        writers=document_writers,
        renderer=document_renderer,
        templates=document_templates,
        sync_notifier=document_sync_worker.notify,
    )
    plan_service = ChangePlanService(settings=settings, file_store=files, state_store=state)
    execution_service = ChangeExecutionService(
        catalog=catalog,
        file_store=files,
        state_store=state,
        lock_manager=locks,
        pipeline=pipeline,
        sync_notifier=sync_worker.notify,
    )
    agent = build_agent_gateway(settings, document_reader=document_service)
    chat = ChatService(
        agent=agent,
        plans=plan_service,
        document_plans=document_service,
        state=state,
    )
    return AppContainer(
        settings=settings,
        workspace=workspace,
        state=state,
        resolver=resolver,
        files=files,
        document_files=document_files,
        catalog=catalog,
        pipeline=pipeline,
        sync_worker=sync_worker,
        document_sync_worker=document_sync_worker,
        document_service=document_service,
        plans=plan_service,
        executions=execution_service,
        agent=agent,
        chat=chat,
        authenticator=build_authenticator(settings),
        doc2md=doc2md,
        reset_lock=asyncio.Lock(),
    )
