/**
 * doc2md child에 필요한 OS/runtime 설정만 전달한다.
 * OAuth client secret 같은 Electron/cloud 자격증명은 이 경계를 넘지 않는다.
 */
export function safeProcessEnvironment(baseEnv) {
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
    // doc2md가 OCR 모델/cache를 통합 project 내부의 명시된 위치에 유지하게 한다.
    'DOC2MD_DATA_ROOT',
  ]) {
    if (baseEnv[name] !== undefined) safe[name] = baseEnv[name];
  }
  return safe;
}
