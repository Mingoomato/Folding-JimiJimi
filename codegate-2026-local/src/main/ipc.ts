/**
 * IPC 핸들러 등록 (스펙 v1.4 §2 — 계약의 `IPC` 채널과 1:1).
 * `src/preload/index.ts` 의 `invoke` 목록이 곧 이 파일의 목록이다.
 *
 * 모든 예외는 여기서 한국어 문장으로 바뀌어 렌더러에 전달된다 — raw stack 을 넘기지 않는다
 * (스펙 v1.4 §5 silent fail 금지).
 */
import path from 'node:path';
import { dialog, ipcMain, shell, type BrowserWindow } from 'electron';
import { IPC, type LlmKeyInput, type LlmProvider, type LoginRequest } from '@contracts';
import type { AppServices } from '@main/services';
import { GENERATED_EXTENSION, titleFromUserQuery, uniquePath } from '@main/kordoc/generate';
import { selectHwpTemplate } from '@main/kordoc/template';
import type { DocumentPlanResponse } from '@main/sidecar/client';
import { stableDocumentId } from '@main/sidecar/input-sync';
import { UserFacingError, logError, toUserMessage } from '@main/util/errors';
import { sourceUriToRelativePath } from '@main/util/vpath';

/** 핸들러를 감싸 오류를 한국어 문장으로 정규화한다. */
function handle<T>(
  channel: string,
  fallbackMessage: string,
  fn: (...args: never[]) => T | Promise<T>,
): void {
  ipcMain.handle(channel, async (_event, ...args: unknown[]) => {
    try {
      return await fn(...(args as never[]));
    } catch (err) {
      logError(`ipc:${channel}`, err);
      throw new Error(toUserMessage(err, fallbackMessage));
    }
  });
}

export function registerIpcHandlers(
  services: AppServices,
  getWindow: () => BrowserWindow | null,
): void {
  /* ------------------------------------------------------------- L4 인증 */

  handle(IPC.authLogin, '로그인에 실패했습니다.', (request: LoginRequest) =>
    services.auth.login(request),
  );
  handle(IPC.authLogout, '로그아웃에 실패했습니다.', () => services.auth.logout());
  handle(IPC.authSession, '세션 정보를 불러오지 못했습니다.', () => services.auth.getSession());

  /* ------------------------------------------------------------- L4 폴더 */

  handle(IPC.rootsList, '등록된 폴더를 불러오지 못했습니다.', () => services.listRoots());

  handle(IPC.rootsPickDialog, '폴더 선택 창을 열지 못했습니다.', async () => {
    const window = getWindow();
    const result = window
      ? await dialog.showOpenDialog(window, {
          title: '문서 폴더 선택',
          buttonLabel: '이 폴더 등록',
          properties: ['openDirectory', 'createDirectory'],
        })
      : await dialog.showOpenDialog({ properties: ['openDirectory'] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });

  handle(IPC.rootsScanPreview, '폴더를 훑어보지 못했습니다.', (rootPath: string) =>
    services.scanPreview(rootPath),
  );
  handle(IPC.rootsAdd, '폴더를 등록하지 못했습니다.', (rootPath: string) =>
    services.addRoot(rootPath),
  );
  handle(IPC.rootsRemove, '폴더를 삭제하지 못했습니다.', (id: string) => services.removeRoot(id));

  /* ------------------------------------------------------------- L2 빌드 */

  // 빌드는 오래 걸리므로 즉시 반환하고, 진행 상황은 IPC_EVENTS.buildState 로 흘린다.
  // 실제로 답변에 쓰이는 위키를 만드는 쪽(sidecar 파이프라인)을 돌린다 — 근거는
  // AppServices.requestManualBuild 주석 참고.
  handle(IPC.buildTrigger, '빌드를 시작하지 못했습니다.', () => {
    services.requestManualBuild();
  });
  handle(IPC.buildState, '빌드 상태를 불러오지 못했습니다.', () => services.getBuildState());

  /* ------------------------------------ L4 enrichment LLM 키 (자격증명 ②) */

  // 원문 키는 어떤 채널로도 렌더러에 내려가지 않는다 — status 는 설정 여부와 끝 4자리뿐이다.
  handle(IPC.llmKeyStatus, 'LLM 키 상태를 불러오지 못했습니다.', () =>
    services.llmKeys.status(),
  );

  handle(IPC.llmKeySet, 'LLM 키를 저장하지 못했습니다.', (input: LlmKeyInput) =>
    services.setLlmKey(input),
  );

  handle(IPC.llmKeyClear, 'LLM 키를 삭제하지 못했습니다.', (provider: LlmProvider) =>
    services.clearLlmKey(provider),
  );

  /* ------------------------------------------------------------- L1 트리 */

  handle(IPC.treeGet, '문서 목록을 불러오지 못했습니다.', () => services.getTree());

  /* ------------------------------------------------------------- L1 채팅 */

  handle(IPC.chatList, '채팅 목록을 불러오지 못했습니다.', () => services.chat.list());
  handle(IPC.chatCreate, '새 채팅을 만들지 못했습니다.', () => services.chat.create());
  handle(IPC.chatDelete, '채팅을 삭제하지 못했습니다.', (conversationId: string) =>
    services.chat.delete(conversationId),
  );
  handle(IPC.chatMessages, '대화 내용을 불러오지 못했습니다.', (conversationId: string) =>
    services.chat.messages(conversationId),
  );
  handle(IPC.chatSend, '메시지를 보내지 못했습니다.', (conversationId: string, text: string) =>
    services.sendChat(conversationId, text),
  );
  handle(IPC.chatAbort, '응답을 중단하지 못했습니다.', (conversationId: string) => {
    services.chat.abort(conversationId);
  });

  handle(
    IPC.executionRetry,
    '문서 동기화를 다시 시작하지 못했습니다.',
    (messageId: string, executionId: string) => services.retryExecution(messageId, executionId),
  );
  handle(
    IPC.executionUndo,
    '문서 변경을 되돌리지 못했습니다.',
    (messageId: string, executionId: string) => services.undoExecution(messageId, executionId),
  );

  /* --------------------------------------------------------- L1 승인 응답 */

  handle(IPC.approvalRespond, '승인 결과를 전달하지 못했습니다.', (id: string, approved: boolean) => {
    services.approval.respond(id, approved);
  });

  /* ----------------------------------------------------- L1 지식 그래프 */

  handle(IPC.wikiGraph, '지식 그래프를 불러오지 못했습니다.', () => services.getWikiGraph());

  /* ------------------------------------------------------- L1 원본 열기 */

  handle(IPC.openOriginal, '원본 문서를 열지 못했습니다.', async (virtualPath: string) => {
    const absPath = services.resolveOriginal(virtualPath);
    if (!absPath) {
      throw new Error('등록된 폴더 안에서 원본을 찾지 못했습니다.');
    }
    const failure = await shell.openPath(absPath);
    if (failure) throw new Error(`원본 문서를 열지 못했습니다: ${failure}`);
  });

  /*
   * 파일 탐색기에서 보여주기 — 여는 것과 용도가 다르다.
   * 문서를 고치면 옆에 `.bak` 백업이 생기는데, 파일을 여는 것만으로는 그 백업이 보이지 않는다.
   * 되돌리려면 폴더를 봐야 하므로 별도 동작으로 둔다.
   */
  handle(IPC.revealOriginal, '폴더에서 문서를 찾지 못했습니다.', (virtualPath: string) => {
    const absPath = services.resolveOriginal(virtualPath);
    if (!absPath) {
      throw new Error('등록된 폴더 안에서 원본을 찾지 못했습니다.');
    }
    shell.showItemInFolder(absPath);
  });

  /* ------------------------------------------- L2 답변을 한글 문서로 저장 */

  /*
   * 저장 위치는 **사용자가 고른다.** 등록 폴더에 조용히 떨어뜨리지 않는다 —
   * 새 파일을 만드는 일은 되돌릴 원본이 없어서, 어디에 무엇이 생기는지 보이는 편이 낫다.
   * 저장하면 watcher 가 잡아 다음 빌드에 자연스럽게 들어온다.
   */
  handle(IPC.documentSaveHwpx, '한글 문서를 저장하지 못했습니다.', async (
    markdown: string,
    templateSourcePath?: string,
    userQuery?: string,
  ) => {
    const window = getWindow();
    const rootPath = services.primaryRootPath();
    if (!rootPath) throw new Error('먼저 문서를 저장할 작업 폴더를 등록해 주세요.');
    let templatePath = templateSourcePath
      ? services.resolveOriginal(templateSourcePath)
      : null;
    const indexedFiles = services.store.listFiles();
    if (!templatePath || path.extname(templatePath).toLowerCase() !== '.hwp') {
      templatePath = selectHwpTemplate(indexedFiles, userQuery ?? markdown)?.absPath ?? null;
    }
    if (!templatePath || path.extname(templatePath).toLowerCase() !== '.hwp') {
      const picked = await dialog.showOpenDialog(window ?? undefined!, {
        title: '사용할 한글 양식 선택',
        defaultPath: rootPath,
        properties: ['openFile'],
        filters: [{ name: '한글 양식', extensions: ['hwp'] }],
      });
      if (picked.canceled || picked.filePaths.length !== 1) return null;
      templatePath = picked.filePaths[0]!;
    }
    const templateRelativePath = confinedSourceRelativePath(rootPath, templatePath, '.hwp');
    const graph = await services.getWikiGraph();
    const templateDocument = graph.nodes.find((node) => {
      const relative = node.sourceUri ? sourceUriToRelativePath(node.sourceUri) : null;
      return relative?.replaceAll('\\', '/') === templateRelativePath;
    });
    const indexedTemplate = indexedFiles.find(
      (file) => path.resolve(file.absPath).toLowerCase() === path.resolve(templatePath).toLowerCase(),
    );
    if (!templateDocument || !indexedTemplate || indexedTemplate.status !== 'done') {
      throw new UserFacingError('선택한 HWP 양식이 현재 LLMWIKI 빌드에 없습니다. 다시 빌드한 뒤 저장해 주세요.');
    }
    const suggested = `${titleFromUserQuery(userQuery, markdown)}${GENERATED_EXTENSION}`;
    const suggestedTarget = await uniquePath(
      rootPath,
      path.basename(suggested, GENERATED_EXTENSION),
      GENERATED_EXTENSION,
    );
    const result = await dialog.showSaveDialog(window ?? undefined!, {
      title: '한글 문서로 저장',
      defaultPath: suggestedTarget,
      filters: [{ name: '한글 문서', extensions: ['hwpx'] }],
    });
    if (result.canceled || !result.filePath) return null;

    const targetRelativePath = confinedRelativePath(rootPath, result.filePath, '.hwpx');
    const registry = await services.sidecar.documentCapabilities();
    const capability = registry.capabilities.find(
      (item) => item.format === 'hwp' && item.active && item.create
        && item.operations.includes('hwp.derive_hwpx/v1'),
    );
    if (!capability) {
      const reason = registry.capabilities
        .find((item) => item.format === 'hwp')
        ?.disabled_reasons.join('; ');
      throw new UserFacingError(reason || 'HWP를 편집 가능한 HWPX로 변환하는 기능을 사용할 수 없습니다.');
    }
    const health = await services.sidecar.health();
    const queued = await services.sidecar.createDocumentPlan({
      document_id: stableDocumentId(targetRelativePath),
      format: 'hwpx',
      capability_id: capability.capability_id,
      capability_snapshot_id: registry.snapshot_id,
      graph_version: health.knowledge_version,
      target_relative_path: targetRelativePath,
      payload: {
        type: 'hwp.derive_hwpx/v1',
        source_document_id: templateDocument.docId,
        expected_source_sha256: indexedTemplate.sha256,
        markdown,
      },
    });
    const plan = await services.sidecar.waitForDocumentPlan(queued);
    requirePendingApproval(plan);
    const images = await Promise.all(
      (plan.preview_manifest?.pairs ?? []).map(async (pair) => ({
        label: pair.locator_label,
        beforeDataUrl: pair.before_artifact_id
          ? await services.sidecar.previewDataUrl(plan.change_plan_id, pair.before_artifact_id)
          : undefined,
        afterDataUrl: pair.after_artifact_id
          ? await services.sidecar.previewDataUrl(plan.change_plan_id, pair.after_artifact_id)
          : undefined,
        summaryOnly: pair.summary_only,
      })),
    );
    const approved = await services.approval.request({
      tool: 'create_from_template',
      target: targetRelativePath,
      diff: JSON.stringify(plan.structural_diff, null, 2),
      rationale: `「${templateRelativePath}」 원본은 보존합니다. 편집 가능한 HWPX 사본으로 변환한 뒤 양식에 내용을 채우고, 승인된 결과만 CREATE_NEW로 생성하여 LLMWIKI를 재색인합니다.`,
      changePlanId: plan.change_plan_id,
      planHash: plan.plan_hash ?? undefined,
      documentPreview: {
        format: plan.format,
        capabilityId: plan.capability_id,
        writerFingerprint: plan.writer_fingerprint ?? undefined,
        rendererFingerprint: plan.renderer_fingerprint ?? undefined,
        sourceSha256: plan.base_sha256 ?? undefined,
        proposedSha256: plan.proposed_sha256 ?? undefined,
        targetRelativePath,
        warnings: plan.warnings,
        structuralDiff: plan.structural_diff.map((item) => ({
          operationIndex: item.operation_index,
          operationType: item.operation_type,
          locator: item.locator,
          before: item.before,
          after: item.after,
        })),
        images,
        truncatedCount: plan.preview_manifest?.truncated_count ?? 0,
      },
    });
    if (!approved) {
      await services.sidecar.rejectDocumentPlan(plan);
      return null;
    }
    if (!plan.proposed_sha256) {
      throw new Error('승인할 HWPX artifact hash가 없습니다. 계획을 다시 만들어 주세요.');
    }
    const cancelManagedChange = services.expectManagedDocumentChange(
      targetRelativePath,
      plan.proposed_sha256,
    );
    try {
      const execution = await services.sidecar.approveDocumentPlan(plan);
      const completed = await services.sidecar.waitForDocumentExecution(execution);
      if (completed.status === 'failed' || completed.status === 'conflict') {
        throw new Error(completed.error?.message || '승인된 HWPX 문서를 적용하지 못했습니다.');
      }
    } finally {
      cancelManagedChange();
    }
    return result.filePath;
  });

  /*
   * 양식을 복사해 채운다.
   *
   * 원본 양식은 읽기만 한다 — 일일업무일지처럼 빈 양식을 두고 매번 새 사본을 채우는
   * 문서가 있어서, 원본을 고치면 다음에 쓸 양식이 사라진다. 저장 경로도 사용자가 고른다.
   */
  handle(IPC.documentFillTemplate, '양식을 채우지 못했습니다.', async (markdown: string) => {
    void markdown;
    throw new Error(
      '직접 template writer는 비활성화되었습니다. 승인된 template_id registry를 통한 API v2 생성만 허용됩니다.',
    );
  });
}

function confinedRelativePath(rootPath: string, targetPath: string, suffix: string): string {
  const root = path.resolve(rootPath);
  const target = path.resolve(targetPath);
  const relative = path.relative(root, target);
  if (
    !relative ||
    path.isAbsolute(relative) ||
    relative === '..' ||
    relative.startsWith(`..${path.sep}`) ||
    path.extname(target).toLowerCase() !== suffix
  ) {
    throw new Error('생성 파일은 등록된 작업 폴더 내부의 올바른 확장자 경로여야 합니다.');
  }
  return relative.split(path.sep).join('/');
}

function confinedSourceRelativePath(rootPath: string, sourcePath: string, suffix: string): string {
  const root = path.resolve(rootPath);
  const source = path.resolve(sourcePath);
  const relative = path.relative(root, source);
  if (
    !relative ||
    path.isAbsolute(relative) ||
    relative === '..' ||
    relative.startsWith(`..${path.sep}`) ||
    path.extname(source).toLowerCase() !== suffix
  ) {
    throw new Error('양식 파일은 등록된 작업 폴더 안의 올바른 HWP 파일이어야 합니다.');
  }
  return relative.split(path.sep).join('/');
}

function requirePendingApproval(plan: DocumentPlanResponse): void {
  if (plan.status === 'pending_approval' && plan.plan_hash) return;
  throw new Error(plan.error?.message || `문서 계획을 승인할 수 없습니다 (${plan.status}).`);
}

/** 창이 닫힐 때 핸들러를 정리한다 (재기동 시 중복 등록 방지). */
export function unregisterIpcHandlers(): void {
  for (const channel of Object.values(IPC)) ipcMain.removeHandler(channel);
}
