/**
 * Electron 메인 프로세스 부트스트랩 (스펙 v1.3 §0 — 메인이 곧 에이전트 호스트).
 * 창을 띄우고, 서비스 컨테이너를 조립하고, IPC 핸들러를 등록한다.
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { app, BrowserWindow, shell } from 'electron';
import { loadDotEnv } from '@main/env';
import { AppServices } from '@main/services';
import { registerIpcHandlers, unregisterIpcHandlers } from '@main/ipc';
import { logError } from '@main/util/errors';

/** ESM/CJS 어느 형식으로 번들되든 현재 디렉터리를 얻는다. */
const currentDir =
  typeof __dirname !== 'undefined' ? __dirname : path.dirname(fileURLToPath(import.meta.url));

/*
 * 설정은 `.env` 하나로 읽는다 — 어떻게 띄우든 같은 파일을 본다.
 * 서비스 조립보다 **먼저** 불러야 한다: AppServices.create 가 process.env 를 읽는다.
 * 설치본에서는 앱 리소스 옆의 `.env` 를 본다 (cwd 는 사용자 홈일 수 있다).
 */
const dotenv = loadDotEnv(app.isPackaged ? process.resourcesPath : process.cwd());
console.info(
  dotenv.file
    ? `[codegate:env] ${dotenv.file} 적용 — ${dotenv.applied.length}개 (${dotenv.applied.join(', ')})`
    : '[codegate:env] .env 없음 — 셸 환경변수만 사용합니다.',
);

let mainWindow: BrowserWindow | null = null;
let services: AppServices | null = null;
let allowQuit = false;
let cleanupStarted = false;

/** 렌더러로 단방향 푸시 — 창이 없으면 조용히 버린다. */
function send(channel: string, payload: unknown): void {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 1040,
    minHeight: 680,
    show: false,
    backgroundColor: '#F7F8FA',
    titleBarStyle: 'hiddenInset',
    autoHideMenuBar: true,
    webPreferences: {
      // package.json 이 "type": "module" 이라 electron-vite 는 preload 를 .mjs 로 내보낸다.
      // .js 를 가리키면 파일이 없어 preload 가 통째로 로드되지 않고,
      // window.codegate 가 undefined 가 되어 렌더러가 첫 렌더에서 죽는다(하얀 화면).
      preload: path.join(currentDir, '../preload/index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      // preload 에서 electron 모듈을 쓰기 위해 sandbox 는 끈다 (contextIsolation 은 유지)
      sandbox: false,
    },
  });

  window.on('ready-to-show', () => window.show());

  // 외부 링크는 앱 안에서 열지 않는다
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: 'deny' };
  });

  const devServerUrl = process.env['ELECTRON_RENDERER_URL'];
  if (devServerUrl) {
    void window.loadURL(devServerUrl);
  } else {
    void window.loadFile(path.join(currentDir, '../renderer/index.html'));
  }

  return window;
}

async function bootstrap(): Promise<void> {
  mainWindow = createWindow();

  services = await AppServices.create({
    userDataDir: app.getPath('userData'),
    homeDir: app.getPath('home'),
    resourcesPath: process.resourcesPath,
    isPackaged: app.isPackaged,
    projectDir: process.cwd(),
    send,
  });

  registerIpcHandlers(services, () => mainWindow);

  // 저장된 토큰 복원 — 결과는 IPC_EVENTS.sessionChanged 로 렌더러에 전달된다
  await services.auth.restore().catch((err: unknown) => logError('auth:restore', err));

  // 첫 렌더 시 트리를 한 번 밀어준다
  mainWindow.webContents.once('did-finish-load', () => services?.emitTree());
}

// 단일 인스턴스 — 두 번째 실행은 기존 창을 살린다
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(
    () => {
      void bootstrap().catch((err: unknown) => logError('bootstrap', err));

      app.on('activate', () => {
        // macOS — 독 아이콘 클릭 시 창이 없으면 다시 만든다
        if (BrowserWindow.getAllWindows().length === 0) {
          mainWindow = createWindow();
        }
      });
    },
    (err: unknown) => logError('whenReady', err),
  );

  app.on('window-all-closed', () => {
    // macOS 는 명시적으로 종료할 때까지 앱을 살려둔다
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', (event) => {
    if (allowQuit) return;
    event.preventDefault();
    if (cleanupStarted) return;
    cleanupStarted = true;
    unregisterIpcHandlers();
    void (services?.dispose() ?? Promise.resolve())
      .catch((err: unknown) => logError('dispose', err))
      .finally(() => {
        allowQuit = true;
        app.quit();
      });
  });
}
