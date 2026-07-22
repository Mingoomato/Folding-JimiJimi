#!/usr/bin/env bash
# Folding 디자인 시스템 웹폰트를 앱에 번들한다.
# codegate는 오프라인에서도 동작해야 하므로(위키가 로컬에만 존재) CDN 링크 대신
# 바이너리를 받아 src/renderer/public/fonts/ 에 둔다. 결과물은 git에 커밋된다.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/renderer/public/fonts"
mkdir -p "$DIR"

PRETENDARD="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/packages/pretendard/dist/web/variable/woff2/PretendardVariable.woff2"
MONO_BASE="https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.0.18/files"

echo "→ Pretendard (variable)"
curl -fsSL -o "$DIR/PretendardVariable.woff2" "$PRETENDARD"

for w in 400 500 700; do
  echo "→ JetBrains Mono $w"
  curl -fsSL -o "$DIR/jetbrains-mono-latin-$w-normal.woff2" \
    "$MONO_BASE/jetbrains-mono-latin-$w-normal.woff2"
done

echo "완료:"
ls -la "$DIR"
