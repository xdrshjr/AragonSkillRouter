// 生成 build/icon.png（1024×1024）——纯 Node 实现，**不引入任何依赖**（spec §2.13）。
//
// 设计文档允许「@resvg/resvg-js 不可用则接受默认图标」，但那会让 electron-builder 的
// `win.icon` 指向不存在的文件而直接失败。这里改成用 zlib + 手写 PNG chunk 画一个
// 圆角渐变的品牌占位图：确定性、零依赖、可复现。
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(path.resolve(here, '..'), 'build');
const outFile = path.join(outDir, 'icon.png');
const SIZE = 1024;

function crc32(buf) {
  let c;
  const table = crc32.table ?? (crc32.table = (() => {
    const t = new Int32Array(256);
    for (let n = 0; n < 256; n += 1) {
      c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c;
    }
    return t;
  })());
  let crc = -1;
  for (let i = 0; i < buf.length; i += 1) crc = table[(crc ^ buf[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ -1) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, 'ascii');
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crcBuf]);
}

function png(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (width * 4 + 1)] = 0; // filter: none
    rgba.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function build() {
  const px = Buffer.alloc(SIZE * SIZE * 4);
  const radius = SIZE * 0.22;
  for (let y = 0; y < SIZE; y += 1) {
    for (let x = 0; x < SIZE; x += 1) {
      const i = (y * SIZE + x) * 4;
      // 圆角矩形遮罩
      const dx = Math.max(radius - x, x - (SIZE - 1 - radius), 0);
      const dy = Math.max(radius - y, y - (SIZE - 1 - radius), 0);
      const dist = Math.hypot(dx, dy);
      const alpha = dist <= radius ? 255 : Math.max(0, 255 - (dist - radius) * 12);

      // 对角渐变（#4f8cff → #7d5cff）
      const t = (x / SIZE) * 0.45 + (y / SIZE) * 0.55;
      let r = Math.round(79 + (125 - 79) * t);
      let g = Math.round(140 + (92 - 140) * t);
      let b = Math.round(255 + (255 - 255) * t);

      // 中央的「路由分叉」标识：两条白色斜线 + 一个圆点
      const cx = SIZE / 2;
      const cy = SIZE / 2;
      const nearLine1 = Math.abs(y - (SIZE * 0.5)) < SIZE * 0.022 && x > SIZE * 0.24 && x < SIZE * 0.76;
      const dot = Math.hypot(x - cx, y - (SIZE * 0.5)) < SIZE * 0.055;
      const branch =
        Math.abs(y - (SIZE * 0.5 + (x - cx) * 0.55)) < SIZE * 0.018 && x > cx && x < SIZE * 0.76;
      if (nearLine1 || dot || branch) {
        r = 245;
        g = 248;
        b = 255;
      }

      px[i] = r;
      px[i + 1] = g;
      px[i + 2] = b;
      px[i + 3] = Math.round(alpha);
    }
  }
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(outFile, png(SIZE, SIZE, px));
  console.log(`[make-icon] ${outFile} (${SIZE}x${SIZE})`);
}

build();
