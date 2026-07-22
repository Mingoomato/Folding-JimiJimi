function settleExit(name, code, signal, resolve, reject) {
  if (signal === 'SIGINT' || signal === 'SIGTERM' || code === 0) {
    resolve();
    return;
  }
  reject(new Error(`${name} process가 종료 코드 ${code ?? signal}로 끝났습니다.`));
}

export function waitForTrackedExit(tracked) {
  return new Promise((resolve, reject) => {
    if (tracked.spawnError) {
      reject(tracked.spawnError);
      return;
    }
    const { exitCode, signalCode } = tracked.child;
    if (exitCode !== null || signalCode !== null) {
      settleExit(tracked.name, exitCode, signalCode, resolve, reject);
      return;
    }
    tracked.child.once('error', reject);
    tracked.child.once('exit', (code, signal) => {
      settleExit(tracked.name, code, signal, resolve, reject);
    });
  });
}
