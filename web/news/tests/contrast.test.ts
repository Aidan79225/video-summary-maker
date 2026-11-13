/**
 * 影片頁新加的兩處文字顏色，深色與淺色兩種配色下都要有 4.5:1（WCAG 2.x AA，一般大小的文字）。
 *
 * 色值不寫死，直接讀 src/styles/global.css 的設計 token：之後改了 token、對比掉下去，這裡就會紅。
 * 每個字色疊在它實際會疊的底上：半透明的底（accent-soft）先疊到頁面底色上。
 * 頁面上用了哪個 class（有沒有被透明度淡掉）由 pages.test.ts 看實際輸出的 HTML。
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

type Rgba = [number, number, number, number];

const CSS = readFileSync(fileURLToPath(new URL('../src/styles/global.css', import.meta.url)), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
);

function tokens(block: RegExp): Map<string, string> {
  const m = block.exec(CSS);
  assert.ok(m, `global.css 裡找不到 ${block}`);
  const out = new Map([...(m[1] ?? '').matchAll(/(--c-[\w-]+)\s*:\s*([^;]+);/g)].map((t) => [t[1] ?? '', (t[2] ?? '').trim()]));
  assert.ok(out.size > 8, `token 讀得太少：${out.size}`);
  return out;
}

/** 深色是預設的 :root；淺色只覆蓋它改的那幾個（prefers-color-scheme: light 裡的 :root） */
const DARK = tokens(/^:root\s*\{([^}]*)\}/m);
const LIGHT = new Map([...DARK, ...tokens(/@media \(prefers-color-scheme: light\)\s*\{\s*:root\s*\{([^}]*)\}/)]);

function parse(value: string): Rgba {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const n = parseInt(hex[1] ?? '', 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  const rgba = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(value);
  assert.ok(rgba, `看不懂的顏色：${value}`);
  return [Number(rgba[1]), Number(rgba[2]), Number(rgba[3]), rgba[4] === undefined ? 1 : Number(rgba[4])];
}

/** fg 以它自己的不透明度疊在不透明的 bg 上 */
function over(fg: Rgba, bg: Rgba): Rgba {
  const [r, g, b, a] = fg;
  return [r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a), 1];
}

const channel = (c: number) => {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};
const luminance = ([r, g, b]: Rgba) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
function ratio(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
}

describe('對比公式本身', () => {
  it('黑對白 21:1；#777 對白剛好不到 4.5', () => {
    assert.equal(ratio([0, 0, 0, 1], [255, 255, 255, 1]).toFixed(2), '21.00');
    assert.ok(ratio(parse('#777777'), parse('#ffffff')) < 4.5);
    assert.ok(ratio(parse('#767676'), parse('#ffffff')) >= 4.5);
  });
});

for (const [scheme, t] of [
  ['深色', DARK],
  ['淺色', LIGHT],
] as const) {
  const color = (name: string) => parse(t.get(`--c-${name}`) ?? assert.fail(`沒有 --c-${name}`));

  describe(`${scheme}配色`, () => {
    it('頻道晶片的字（頻道名稱與「頻道」標籤，都是 accent-ink 全強度）疊在 accent-soft 的底上：至少 4.5:1', () => {
      const pill = over(color('accent-soft'), color('bg'));
      const value = ratio(color('accent-ink'), pill);
      assert.ok(value >= 4.5, `${scheme}：${value.toFixed(2)}:1`);
    });

    it('卡片頁尾的「N 位說話者」（全強度的 muted）疊在卡片底（surface）上：至少 4.5:1', () => {
      const value = ratio(color('muted'), color('surface'));
      assert.ok(value >= 4.5, `${scheme}：${value.toFixed(2)}:1`);
    });
  });
}
