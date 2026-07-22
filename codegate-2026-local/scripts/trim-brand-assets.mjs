#!/usr/bin/env node
/**
 * Folding 브랜드 자산 트리밍.
 *
 * 디자인 프로젝트에서 받은 원본(1254×1254)은 로고 주위에 넓은 빈 여백이 있다.
 * 그대로 26px 로 줄여 쓰면 실제 마크는 8px 남짓으로 보인다.
 * 그래서 "잉크가 있는 영역"의 바운딩 박스를 구해 잘라낸 뒤 앱에 번들한다.
 *
 * 투명 픽셀과 흰 배경 둘 다 여백으로 본다 (원본은 alpha 를 갖지만
 * 실제로는 흰색으로 채워져 있을 수 있다).
 *
 * 실행: node scripts/trim-brand-assets.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PNG } from 'pngjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outDir = path.join(root, 'src/renderer/public/brand');

/** 원본 → 출력 이름 */
const SOURCES = [
  { src: process.argv[2], out: 'folding-mark.png' },
  { src: process.argv[3], out: 'folding-logo.png' },
];

/** 여백으로 볼지 판단 — 투명하거나 거의 흰색이면 여백. */
function isBlank(r, g, b, a) {
  if (a < 8) return true;
  return r > 246 && g > 246 && b > 246;
}

function trim(srcPath, outPath) {
  const png = PNG.sync.read(fs.readFileSync(srcPath));
  const { width, height, data } = png;

  let top = height,
    left = width,
    right = -1,
    bottom = -1;

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = (width * y + x) << 2;
      if (isBlank(data[i], data[i + 1], data[i + 2], data[i + 3])) continue;
      if (x < left) left = x;
      if (x > right) right = x;
      if (y < top) top = y;
      if (y > bottom) bottom = y;
    }
  }

  if (right < 0) throw new Error(`${srcPath}: 잉크가 있는 픽셀을 찾지 못했습니다.`);

  // 시각적 여유 — 잘린 느낌이 나지 않게 짧은 변의 1.5% 만 남긴다
  const pad = Math.round(Math.min(right - left, bottom - top) * 0.015);
  left = Math.max(0, left - pad);
  top = Math.max(0, top - pad);
  right = Math.min(width - 1, right + pad);
  bottom = Math.min(height - 1, bottom + pad);

  const w = right - left + 1;
  const h = bottom - top + 1;
  const out = new PNG({ width: w, height: h });

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const s = (width * (y + top) + (x + left)) << 2;
      const d = (w * y + x) << 2;
      out.data[d] = data[s];
      out.data[d + 1] = data[s + 1];
      out.data[d + 2] = data[s + 2];
      // 흰 배경을 투명하게 — 어두운 면(로그인 레일) 위에서도 쓸 수 있어야 한다
      const blank = isBlank(data[s], data[s + 1], data[s + 2], data[s + 3]);
      out.data[d + 3] = blank ? 0 : data[s + 3];
    }
  }

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, PNG.sync.write(out));
  console.log(`${path.basename(outPath)}  ${width}×${height} → ${w}×${h}`);
}

for (const { src, out } of SOURCES) {
  if (!src) throw new Error('사용법: node scripts/trim-brand-assets.mjs <mark.png> <logo.png>');
  trim(src, path.join(outDir, out));
}
