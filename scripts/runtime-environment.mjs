import path from 'node:path';

export const FIXED_CALLBACK = 'http://127.0.0.1:47821/auth/callback';

export function buildRuntimeEnvironments(baseEnv, paths) {
  const systemEnv = pickSystemEnvironment(baseEnv);
  const cloudEnv = {
    ...systemEnv,
    CODEGATE_ENVIRONMENT: 'development',
    CODEGATE_API_HOST: '127.0.0.1',
    CODEGATE_API_PORT: '8000',
    CODEGATE_DATABASE_PATH: path.join(paths.runtimeDir, 'cloud-api.sqlite3'),
    CODEGATE_CORS_ORIGINS: '["http://localhost:5173","http://127.0.0.1:5173"]',
    CODEGATE_GOOGLE_CLIENT_ID: baseEnv.CODEGATE_GOOGLE_CLIENT_ID,
    CODEGATE_GOOGLE_CLIENT_SECRET: baseEnv.CODEGATE_GOOGLE_CLIENT_SECRET,
    CODEGATE_SESSION_PEPPER: baseEnv.CODEGATE_SESSION_PEPPER,
  };
  const desktopEnv = {
    ...systemEnv,
    GEMINI_API_KEY: baseEnv.GEMINI_API_KEY,
    CODEGATE_AGENT_MODE: 'gemini',
    CODEGATE_GEMINI_MODEL: 'gemini-2.5-flash-lite',
    CODEGATE_GEMINI_DATA_POLICY: 'paid-no-training',
    ...(baseEnv.CODEGATE_GEMINI_TIMEOUT_SECONDS
      ? { CODEGATE_GEMINI_TIMEOUT_SECONDS: baseEnv.CODEGATE_GEMINI_TIMEOUT_SECONDS }
      : {}),
    CODEGATE_AUTH_MODE: 'cloud',
    CODEGATE_CLOUD_API_URL: 'http://127.0.0.1:8000/api/v1',
    CODEGATE_OAUTH_REDIRECT_URI: FIXED_CALLBACK,
    CODEGATE_DOC2MD_PROJECT_ROOT: paths.convertDir,
    CODEGATE_DOC2MD_PYTHON: paths.doc2mdPython,
    CODEGATE_SIDECAR_BIN: paths.sidecarBin,
    CODEGATE_LLMWIKI_PROJECT_ROOT: paths.backendDir,
    CODEGATE_LLMWIKI_STARTUP_ENRICHMENT: 'blocking',
    CODEGATE_SIDECAR_READY_TIMEOUT_MS: '300000',
    CODEGATE_NODE_BIN: paths.nodeBin,
    CODEGATE_KORDOC_WORKSPACE_ROOT: paths.localDir,
    CODEGATE_KORDOC_DIR: path.join(paths.localDir, 'node_modules', 'kordoc'),
    DOC2MD_DATA_ROOT: path.join(paths.runtimeDir, 'doc2md'),
    DOC2MD_OCR_DEVICE: 'gpu',
  };
  return { cloudEnv, desktopEnv };
}

export function pickSystemEnvironment(baseEnv) {
  const safe = {};
  for (const name of [
    'PATH',
    'Path',
    'PATHEXT',
    'SystemRoot',
    'SYSTEMROOT',
    'WINDIR',
    'HOME',
    'USERPROFILE',
    'TMPDIR',
    'TMP',
    'TEMP',
    'LANG',
    'LC_ALL',
    'TZ',
    'SSL_CERT_FILE',
    'SSL_CERT_DIR',
    'UV_CACHE_DIR',
    'XDG_CACHE_HOME',
    'NODE_OPTIONS',
    'NO_PROXY',
  ]) {
    if (baseEnv[name] !== undefined) safe[name] = baseEnv[name];
  }
  return safe;
}
