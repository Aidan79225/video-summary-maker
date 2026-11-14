/**
 * 多人影片的頁面：起建置好的站（dist/server/entry.mjs），對 HTML 下斷言。
 *
 * 兩種資料來源：
 *   1. USE_FIXTURE=1：sample.json 的假資料（yt-FIXTURE0003 正常、yt-FIXTURE0004 有警告；
 *      0001／0002 是沒有新欄位的舊樣子，議會文章也沒有）。
 *   2. 一個假的後端（只在這個測試裡、只聽 127.0.0.1）：走正式站的路徑（非假資料），
 *      餵舊版、型別壞掉、還留著 [[Sn]] 的回應。
 *
 * 要先 build：`npm test` 會自己先 build，避免拿舊的 dist 當成過關。
 * 沒有任何外部網路；兩個伺服器都綁在 loopback 的暫用埠。
 */
import { after, before, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { createServer as createHttpServer, type Server } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const ENTRY = fileURLToPath(new URL('../dist/server/entry.mjs', import.meta.url));
const FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL('../src/fixtures/sample.json', import.meta.url)), 'utf8'),
) as { articles: Fixture[] };

type FixturePoint = { speaker: string; display: string; start: number; text: string };
type FixtureSlide = { title: string; points?: FixturePoint[]; bullets: string[]; detail: string };
type Fixture = {
  slug: string;
  source: string;
  speaker: string;
  speakers?: { id: string; display: string; kind: string; basis: string; role: string }[];
  slides: FixtureSlide[];
  transcript_text: string;
};
const fx = (slug: string): Fixture => {
  const a = FIXTURE.articles.find((x) => x.slug === slug);
  assert.ok(a, `假資料裡沒有 ${slug}`);
  return a;
};

const OK = '2026-11-02-yt-FIXTURE0003';
const WARN = '2026-10-20-yt-FIXTURE0004';
const OLD1 = '2026-10-28-yt-FIXTURE0001';
const OLD2 = '2026-09-14-yt-FIXTURE0002';
const COUNCIL = [
  '2026-08-27-900001',
  '2026-08-26-900002',
  '2026-08-26-ntpc-0408R1150826900',
  '2026-08-25-900003',
  '2026-08-25-tccc-900004',
];

/* ------------------------------------------------------------------
   伺服器
   ------------------------------------------------------------------ */

async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createNetServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const addr = s.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      s.close(() => resolve(port));
    });
  });
}

type Site = { base: string; stop: () => Promise<void> };

async function startSite(env: Record<string, string>): Promise<Site> {
  assert.ok(existsSync(ENTRY), '找不到 dist/server/entry.mjs：先跑 npm run build（或直接 npm test）');
  const port = await freePort();
  const child: ChildProcess = spawn(process.execPath, [ENTRY], {
    cwd: ROOT,
    env: { ...process.env, HOST: '127.0.0.1', PORT: String(port), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  child.stdout?.on('data', (d) => (log += String(d)));
  child.stderr?.on('data', (d) => (log += String(d)));
  const base = `http://127.0.0.1:${port}`;
  let up = false;
  for (let i = 0; i < 100 && !up; i++) {
    try {
      // /nope 是 404 頁，不會呼叫 API
      const r = await fetch(`${base}/nope`);
      await r.text();
      up = true;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  if (!up) {
    child.kill();
    throw new Error(`站沒有起來：\n${log}`);
  }
  return {
    base,
    stop: async () => {
      child.kill();
      await new Promise((r) => setTimeout(r, 100));
    },
  };
}

async function get(site: Site, path: string): Promise<{ status: number; html: string }> {
  const res = await fetch(site.base + path, { redirect: 'manual' });
  return { status: res.status, html: await res.text() };
}

/** 假後端：路徑（不含 query）對 JSON；列表另外給函式。只聽 127.0.0.1 */
type Stub = { base: string; detail: Map<string, unknown>; list: { value: unknown }; close: () => Promise<void> };
async function startStub(): Promise<Stub> {
  const detail = new Map<string, unknown>();
  const list = { value: { count: 0, page: 1, page_size: 12, pages: 1, items: [] } as unknown };
  const server: Server = createHttpServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://stub.invalid');
    const send = (status: number, body: unknown) => {
      res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(body));
    };
    const m = /^\/api\/articles\/([^/]+)$/.exec(url.pathname);
    if (m) {
      const key = decodeURIComponent(m[1] ?? '');
      return detail.has(key) ? send(200, detail.get(key)) : send(404, { detail: 'not found' });
    }
    if (url.pathname === '/api/articles') return send(200, list.value);
    return send(404, { detail: 'not found' });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  return {
    base: `http://127.0.0.1:${port}`,
    detail,
    list,
    close: () => new Promise<void>((r) => server.close(() => r())),
  };
}

/* ------------------------------------------------------------------
   HTML 小工具（沒有 DOM 套件，靠頁面上的 data-* 掛鉤）
   ------------------------------------------------------------------ */

const text = (html: string) =>
  html
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ')
    .trim();

/** <tag ... attr ...>…</tag>，裡面不能再有同名的標籤（頁面的掛鉤都照這個設計） */
function elements(html: string, tag: string, attr: string): { open: string; inner: string }[] {
  const re = new RegExp(`<${tag}\\b([^>]*\\b${attr}\\b[^>]*)>([\\s\\S]*?)</${tag}>`, 'g');
  return [...html.matchAll(re)].map((m) => ({ open: m[1] ?? '', inner: m[2] ?? '' }));
}
const attrOf = (open: string, name: string) => new RegExp(`\\b${name}="([^"]*)"`).exec(open)?.[1];
const chipNames = (html: string) => [...html.matchAll(/<span class="spk-name">([^<]*)<\/span>/g)].map((m) => m[1] ?? '');
const count = (html: string, needle: string) => html.split(needle).length - 1;
const unescape = (s: string) => s.replace(/&amp;/g, '&');

const NEUTRAL = '說話者由聲音自動區分，可能分錯；名稱未經確認時只以標籤稱呼';
const DISCLAIMER = '由 AI 依影片自動整理，可能有誤，以原影片為準';
const CAPTION = '畫面僅為該時間點的截圖';

/* ------------------------------------------------------------------
   假資料模式
   ------------------------------------------------------------------ */

describe('假資料模式（USE_FIXTURE=1）', () => {
  let site: Site;
  before(async () => {
    site = await startSite({ USE_FIXTURE: '1' });
  });
  after(async () => {
    await site.stop();
  });

  describe('多人影片（狀態 ok）', () => {
    let html: string;
    before(async () => {
      const r = await get(site, `/article/${OK}`);
      assert.equal(r.status, 200);
      html = r.html;
    });

    it('影片頁原有的東西都還在：noindex、固定的 AI 備註、返回 /videos', () => {
      assert.ok(html.includes('<meta name="robots" content="noindex">'));
      assert.ok(text(html).includes(DISCLAIMER));
      assert.ok(html.includes('href="/videos"'));
    });

    it('說明列：中性的一句話只出現一次，狀態 ok 沒有警告清單', () => {
      const banner = elements(html, 'section', 'data-diarization');
      assert.equal(banner.length, 1);
      assert.equal(attrOf(banner[0]?.open ?? '', 'data-diarization'), 'ok');
      assert.equal(count(text(html), NEUTRAL), 1);
      assert.equal(elements(html, 'li', 'data-warning').length, 0);
    });

    it('出場：參與者在前、短暫的聲音在後；名稱與角色、身分依據都是文字', () => {
      const items = elements(html, 'li', 'data-cast-item');
      assert.deepEqual(
        items.map((i) => [attrOf(i.open, 'data-kind'), attrOf(i.open, 'data-basis')]),
        [
          ['participant', 'operator'],
          ['participant', 'none'],
          ['minor', 'none'],
        ],
      );
      assert.deepEqual(items.map((i) => chipNames(i.inner)), [['範例甲'], ['發言者 B'], ['其他聲音 1']]);
      assert.ok(text(items[0]?.inner ?? '').includes('主持人'));
      assert.ok(text(items[0]?.inner ?? '').includes('已由編輯確認'));
      assert.ok(text(items[1]?.inner ?? '').includes('未確認身分'));
      assert.ok(!text(items[1]?.inner ?? '').includes('已由編輯確認'));
      assert.ok(text(items[2]?.inner ?? '').includes('未確認身分'));
      assert.ok(text(html).includes('出場'));
    });

    it('頻道晶片與說話者分開：寫明「頻道」、連到頻道頁；說話者沒有任何連結', () => {
      const channel = fx(OK).speaker;
      const href = `/speaker/${encodeURIComponent(channel)}?source=youtube`;
      const chip = [...html.matchAll(new RegExp(`<a href="${href.replace(/[?]/g, '\\?')}"[^>]*>([\\s\\S]*?)</a>`, 'g'))];
      assert.ok(chip.length >= 1, '頁首要有頻道連結');
      assert.ok(text(chip[0]?.[1] ?? '').includes('頻道'), '頻道晶片要寫「頻道」');
      assert.ok(text(chip[0]?.[1] ?? '').includes(channel));
      for (const s of fx(OK).speakers ?? []) {
        assert.ok(
          !html.includes(`href="/speaker/${encodeURIComponent(s.display)}`),
          `說話者 ${s.display} 不能連到 /speaker/`,
        );
      }
      // 頁面上所有 /speaker/ 連結都只能是頻道
      for (const m of html.matchAll(/href="(\/speaker\/[^"]*)"/g)) {
        assert.ok((m[1] ?? '').startsWith(`/speaker/${encodeURIComponent(channel)}?`), m[1]);
      }
    });

    it('「頻道」兩個字不能淡掉：不加透明度，字色跟頻道名稱一樣（淺色底 5.7:1，AA 要 4.5:1）', () => {
      const label = elements(html, 'span', 'data-channel-label');
      assert.equal(label.length, 1);
      const cls = attrOf(label[0]?.open ?? '', 'class') ?? '';
      assert.ok(!/(^|\s)opacity-/.test(cls), `標籤的 class 不能帶 opacity：${cls}`);
      assert.ok(!/(^|\s)text-\S+\/\d/.test(cls), `標籤的字色不能帶透明度：${cls}`);
    });

    it('每段的重點：每個重點有說話者晶片、文字、帶 &t= 的連結；不再畫平的條列', () => {
      const slides = fx(OK).slides;
      const sections = [...html.matchAll(/<section\s+id="s(\d+)"[\s\S]*?<\/section>/g)].map((m) => m[0]);
      assert.equal(sections.length, slides.length);
      sections.forEach((sec, i) => {
        const want = slides[i]?.points ?? [];
        const lis = elements(sec, 'li', 'data-point');
        assert.equal(lis.length, want.length, `第 ${i + 1} 段的重點數`);
        lis.forEach((li, j) => {
          const p = want[j];
          assert.ok(p);
          assert.equal(attrOf(li.open, 'data-point'), p.speaker);
          assert.deepEqual(chipNames(li.inner), [p.display]);
          assert.ok(text(li.inner).includes(p.text));
          const href = /href="([^"]+)"/.exec(li.inner)?.[1] ?? '';
          assert.equal(
            unescape(href),
            `https://youtube.example.invalid/watch?v=FIXTURE0003&t=${Math.floor(p.start)}s`,
          );
        });
        assert.ok(!sec.includes('<li class="leading-relaxed">'), '有 points 的段落不畫平的條列');
      });
    });

    it('時間超過一小時的重點：連結秒數與時間文字都對（125:30＝7530 秒）', () => {
      const last = fx(OK).slides.at(-1)?.points?.at(-1);
      assert.equal(last?.start, 7530);
      assert.ok(html.includes('t=7530s'));
      const lis = elements(html, 'li', 'data-point');
      const hit = lis.find((l) => unescape(l.inner).includes('t=7530s'));
      assert.ok(hit);
      assert.ok(text(hit.inner).includes('2:05:30'));
    });

    it('完整敘述還在，內容照 API 給的（名稱已代入）', () => {
      assert.ok(text(html).includes('範例甲：詢問目前補助政策的成效。 發言者 B：現金補助有幫助，但托育資源更重要。'));
    });

    it('每段截圖下面有中性的說明', () => {
      assert.equal(elements(html, 'p', 'data-frame-caption').length, fx(OK).slides.length);
      assert.equal(count(text(html), CAPTION), fx(OK).slides.length);
    });

    it('逐字稿：上方有說話者對照；其他聲音另外說明；相鄰同一位的行併成一塊、晶片只一次', () => {
      const legend = elements(html, 'div', 'data-legend');
      assert.equal(legend.length, 1);
      const items = elements(legend[0]?.inner ?? '', 'li', 'data-legend-item');
      assert.deepEqual(items.map((i) => chipNames(i.inner)), [['範例甲'], ['發言者 B'], ['其他聲音 1']]);
      assert.ok(text(items[0]?.inner ?? '').includes('主持人'));
      assert.ok(text(legend[0]?.inner ?? '').includes('其他聲音'));
      assert.ok(
        text(legend[0]?.inner ?? '').includes('只列在逐字稿裡，不會出現在各段的重點'),
        '「其他聲音」要另外說明：它不會出現在各段的重點',
      );

      const turns = elements(html, 'div', 'data-turn');
      const first = turns[0];
      assert.ok(first);
      assert.deepEqual(chipNames(first.inner), ['範例甲']);
      // 00:14 與 00:26 都是發言者 B，相鄰 → 同一塊
      const second = turns[1];
      assert.deepEqual(chipNames(second?.inner ?? ''), ['發言者 B']);
      assert.ok((second?.inner ?? '').includes('00:14'));
      assert.ok((second?.inner ?? '').includes('00:26'));
      assert.ok(text(second?.inner ?? '').includes('尤其是下班時間的托育'));
      // 每一行逐字稿恰好出現一次
      const lines = fx(OK).transcript_text.split('\n').length;
      assert.equal(count(html, 'class="ts-line"'), lines);
      assert.ok(turns.length < lines, '有併塊');
    });

    it('逐字稿：分鐘超過 99 照樣有時間欄；後端代入的（無法判斷）是中性的晶片，不是某位說話者', () => {
      assert.ok(/<span[^>]*>125:30<\/span>/.test(html));
      const turn = elements(html, 'div', 'data-turn').find((t) => t.inner.includes('70:02'));
      assert.ok(turn);
      assert.equal(attrOf(turn.open, 'data-kind'), 'unknown');
      assert.deepEqual(chipNames(turn.inner), ['（無法判斷）']);
    });

    it('不出現原始佔位符，也沒有診斷用的百分比', () => {
      assert.ok(!html.includes('[[S'));
      const shown = [
        ...elements(html, 'section', 'data-diarization'),
        ...elements(html, 'ul', 'data-cast'),
        ...elements(html, 'div', 'data-legend'),
      ]
        .map((e) => text(e.inner))
        .join(' ');
      assert.ok(shown.length > 0);
      assert.ok(!/[0-9]\s*[%％]/.test(shown), shown);
    });
  });

  describe('多人影片（狀態 warn）', () => {
    let html: string;
    before(async () => {
      const r = await get(site, `/article/${WARN}`);
      assert.equal(r.status, 200);
      html = r.html;
    });

    it('中性的一句話之外，每個警告碼一句白話，依序', () => {
      const banner = elements(html, 'section', 'data-diarization');
      assert.equal(banner.length, 1);
      assert.equal(attrOf(banner[0]?.open ?? '', 'data-diarization'), 'warn');
      assert.equal(count(text(html), NEUTRAL), 1);
      const warnings = elements(html, 'li', 'data-warning');
      assert.deepEqual(
        warnings.map((w) => [attrOf(w.open, 'data-warning'), text(w.inner)]),
        [
          ['count_mismatch', '偵測到的人數與指定的不同。'],
          ['high_unassigned', '有不少片段無法判斷是誰在說話。'],
          ['many_minor', '出現多個短暫的聲音。'],
          ['single_found', '只偵測到一位說話者。'],
        ],
      );
    });

    it('一位參與者與四個短暫的聲音；都沒有確認過身分', () => {
      const items = elements(html, 'li', 'data-cast-item');
      assert.deepEqual(
        items.map((i) => [attrOf(i.open, 'data-kind'), attrOf(i.open, 'data-basis')]),
        [
          ['participant', 'none'],
          ['minor', 'none'],
          ['minor', 'none'],
          ['minor', 'none'],
          ['minor', 'none'],
        ],
      );
      assert.ok(!text(html).includes('已由編輯確認'));
    });

    it('逐字稿：前綴不是已知說話者的那一行原樣當內文，不拆冒號；內文裡的冒號也留著', () => {
      const plain = elements(html, 'div', 'data-turn').find((t) => t.inner.includes('27:45'));
      assert.ok(plain);
      assert.equal(attrOf(plain.open, 'data-kind'), 'none');
      assert.deepEqual(chipNames(plain.inner), []);
      assert.ok(text(plain.inner).includes('現場字幕：這一行的開頭不是任何一位說話者，要原樣顯示。'));
      const colon = elements(html, 'div', 'data-turn').find((t) => t.inner.includes('00:41'));
      assert.ok(text(colon?.inner ?? '').includes('重點有三：第一是申請資格'));
    });

    it('逐字稿上方的說話者對照：五位都列，並說明「其他聲音」只在逐字稿裡', () => {
      const legend = elements(html, 'div', 'data-legend');
      assert.equal(legend.length, 1);
      assert.equal(elements(legend[0]?.inner ?? '', 'li', 'data-legend-item').length, 5);
      assert.ok(text(legend[0]?.inner ?? '').includes('只列在逐字稿裡，不會出現在各段的重點'));
    });

    it('不出現原始佔位符；沒有任何說話者連結', () => {
      assert.ok(!html.includes('[[S'));
      for (const m of html.matchAll(/href="(\/speaker\/[^"]*)"/g)) {
        assert.ok((m[1] ?? '').startsWith(`/speaker/${encodeURIComponent(fx(WARN).speaker)}?`), m[1]);
      }
    });
  });

  describe('沒有新欄位的影片（舊 API 的樣子）照舊', () => {
    for (const slug of [OLD1, OLD2]) {
      it(`${slug}：沒有出場、說明列、說話者晶片、截圖說明；逐字稿與條列是舊的標記`, async () => {
        const { status, html } = await get(site, `/article/${slug}`);
        assert.equal(status, 200);
        for (const hook of ['data-cast', 'data-diarization', 'data-legend', 'data-turn', 'data-point', 'data-points', 'data-frame-caption']) {
          assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), `${slug} 不該有 ${hook}`);
        }
        assert.ok(!html.includes('class="spk'));
        assert.ok(!text(html).includes(CAPTION));
        assert.ok(!text(html).includes(NEUTRAL));
        assert.ok(html.includes('class="ts-line"'));
        assert.ok(html.includes('<li class="leading-relaxed">'));
        assert.ok(text(html).includes(DISCLAIMER));
      });
    }
  });

  describe('議會文章完全沒有新東西', () => {
    for (const slug of COUNCIL) {
      it(`${slug}`, async () => {
        const { status, html } = await get(site, `/article/${slug}`);
        assert.equal(status, 200);
        for (const hook of ['data-cast', 'data-diarization', 'data-legend', 'data-turn', 'data-point', 'data-points', 'data-frame-caption']) {
          assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), `${slug} 不該有 ${hook}`);
        }
        assert.ok(!html.includes('class="spk'));
        // 議會的逐字稿一行一個 ts-line，冒號留在內文
        assert.ok(html.includes('class="ts-line"'));
      });
    }
  });

  describe('/videos 的卡片', () => {
    it('多人影片的卡片寫幾位說話者；舊的影片沒有', async () => {
      const { status, html } = await get(site, '/videos');
      assert.equal(status, 200);
      const cards = [...html.matchAll(/<article\b[\s\S]*?<\/article>/g)].map((m) => m[0]);
      const by = (slug: string) => cards.find((c) => c.includes(`href="/article/${slug}"`)) ?? '';
      assert.ok(text(by(OK)).includes('2 位說話者'));
      // 卡片頁尾繼承的 text-muted/80 在淺色底只有 3.3:1；新加的這一格自己帶全強度的 text-muted（4.8:1）
      const span = elements(by(OK), 'span', 'data-participants')[0];
      assert.ok(/(^|\s)text-muted(\s|$)/.test(attrOf(span?.open ?? '', 'class') ?? ''), '人數那一格要有自己的 text-muted');
      assert.ok(text(by(WARN)).includes('1 位說話者'));
      assert.ok(!text(by(OLD1)).includes('位說話者'));
      assert.ok(!text(by(OLD2)).includes('位說話者'));
      assert.ok(!html.includes('[[S'));
    });

    it('議會的頁面沒有「位說話者」', async () => {
      for (const path of ['/', '/?source=ly', '/?date=2026-08-26']) {
        const { html } = await get(site, path);
        assert.ok(!text(html).includes('位說話者'), path);
      }
    });
  });

  it('沒有任何頁面含有 [[S', async () => {
    const paths = [
      '/',
      '/videos',
      `/article/${OK}`,
      `/article/${WARN}`,
      `/article/${OLD1}`,
      `/article/${OLD2}`,
      ...COUNCIL.map((s) => `/article/${s}`),
      `/speaker/${encodeURIComponent(fx(OK).speaker)}?source=youtube`,
      `/speaker/${encodeURIComponent(fx(WARN).speaker)}?source=youtube`,
    ];
    for (const p of paths) {
      const { html } = await get(site, p);
      assert.ok(!html.includes('[[S'), p);
    }
  });
});

/* ------------------------------------------------------------------
   正式站的路徑（假後端）：舊的、壞的、還留著佔位符的回應
   ------------------------------------------------------------------ */

function videoDetail(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    slug: 'stub-video',
    ivod_id: 'yt-STUB0000001',
    source: 'youtube',
    party: '',
    title: '假後端的影片',
    speaker: '假頻道',
    meeting: '',
    date: '2026-11-01',
    duration_seconds: 600,
    ivod_url: 'https://youtube.example.invalid/watch?v=STUB0000001',
    teaser: '導言',
    cover_image_url: null,
    slide_count: 1,
    source_note: '來源說明',
    transcript_text: '00:05 一句話\n00:20 又一句',
    brief: null,
    slides: [
      {
        index: 1,
        title: '第一段',
        bullets: ['條列一', '條列二'],
        detail: '完整敘述',
        timestamp: 5,
        image_url: null,
      },
    ],
    ...over,
  };
}

describe('正式站的路徑（假後端）', () => {
  let stub: Stub;
  let site: Site;
  before(async () => {
    stub = await startStub();
    site = await startSite({ USE_FIXTURE: '0', PUBLIC_API_BASE: stub.base });
  });
  after(async () => {
    await site.stop();
    await stub.close();
  });

  const NO_NEW = ['data-cast', 'data-diarization', 'data-legend', 'data-turn', 'data-point', 'data-points', 'data-frame-caption'];

  it('舊 API（完全沒有 speakers、diarization、points）：照舊的版面，不爆', async () => {
    stub.detail.set('old', videoDetail());
    const { status, html } = await get(site, '/article/old');
    assert.equal(status, 200);
    for (const hook of NO_NEW) assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), hook);
    assert.ok(html.includes('<li class="leading-relaxed">'));
    assert.ok(html.includes('class="ts-line"'));
  });

  it('新 API 的單人影片（speakers: []、diarization: null、points: []）與舊的一樣', async () => {
    stub.detail.set(
      'single',
      videoDetail({
        speakers: [],
        diarization: null,
        slides: [{ index: 1, title: '第一段', bullets: ['條列一'], detail: '', timestamp: 5, image_url: null, points: [] }],
      }),
    );
    const { status, html } = await get(site, '/article/single');
    assert.equal(status, 200);
    for (const hook of NO_NEW) assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), hook);
    assert.ok(html.includes('<li class="leading-relaxed">'));
  });

  it('型別壞掉（字串、數字、物件代替陣列）：當作沒有，不爆', async () => {
    stub.detail.set(
      'odd',
      videoDetail({
        speakers: 'oops',
        diarization: 5,
        slides: [
          { index: 1, title: '第一段', bullets: ['條列一'], detail: '', timestamp: 5, image_url: null, points: { a: 1 } },
          { index: 2, title: '第二段', bullets: ['條列二'], detail: '', timestamp: 9, image_url: null, points: 'x' },
          null,
          7,
        ],
      }),
    );
    const { status, html } = await get(site, '/article/odd');
    assert.equal(status, 200);
    for (const hook of NO_NEW) assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), hook);
    assert.ok(html.includes('條列一') && html.includes('條列二'));
  });

  it('舊版逐句標記的格式（speakers 是字串陣列）：忽略，不當成說話者', async () => {
    stub.detail.set('strings', videoDetail({ speakers: ['說話者 1', '說話者 2'], diarization: null }));
    const { status, html } = await get(site, '/article/strings');
    assert.equal(status, 200);
    for (const hook of NO_NEW) assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), hook);
  });

  it('議會的文章：就算（壞掉的）API 送了說話者、辨識結果、points，頁面也一個字不變', async () => {
    stub.detail.set(
      'council-extras',
      videoDetail({
        source: 'ly',
        ivod_id: '900099',
        speaker: '範例議員',
        party: '無',
        meeting: '範例會議',
        transcript_text: '00:05 發言者 A：甲說\n00:09 主席：乙說',
        speakers: [
          {
            id: 'S1',
            label: '發言者 A',
            name: '',
            role: '主持人',
            display: '發言者 A',
            kind: 'participant',
            seconds: 1,
            turns: 1,
            basis: 'operator',
            flags: [],
          },
        ],
        diarization: { status: 'warn', warnings: ['many_minor'], requested_speakers: 2, found_speakers: 1 },
        slides: [
          {
            index: 1,
            title: '第一段',
            bullets: ['條列一', '條列二'],
            detail: '',
            timestamp: 5,
            image_url: 'http://stub.invalid/media/a.webp',
            points: [{ speaker: 'S1', display: '發言者 A', start: 5, text: '多人影片才有的重點' }],
          },
        ],
      }),
    );
    const { status, html } = await get(site, '/article/council-extras');
    assert.equal(status, 200);
    for (const hook of NO_NEW) assert.ok(!new RegExp(`\\b${hook}\\b`).test(html), hook);
    assert.ok(!html.includes('class="spk'));
    assert.ok(!text(html).includes(NEUTRAL));
    assert.ok(!text(html).includes(CAPTION));
    assert.ok(!text(html).includes('多人影片才有的重點'), '議會頁面不採用 points');
    assert.ok(html.includes('<li class="leading-relaxed">條列一</li>'), '議會頁面照舊畫條列');
    // 逐字稿：一行一個 ts-line，冒號留在內文，不拆說話者
    assert.equal(count(html, 'class="ts-line"'), 2);
    assert.ok(text(html).includes('發言者 A：甲說'));
  });

  it('重點的說話者名稱跟出場名單對不上（後端自己矛盾）：畫成（未知發言者）；沒有短暫的聲音就沒有那句說明', async () => {
    const mk = (id: string, display: string) => ({
      id,
      label: display,
      name: '',
      role: '',
      display,
      kind: 'participant',
      seconds: 1,
      turns: 1,
      basis: 'none',
      flags: [],
    });
    stub.detail.set(
      'conflict',
      videoDetail({
        speakers: [mk('S1', '發言者 A'), mk('S2', '發言者 B')],
        diarization: { status: 'ok', warnings: [], requested_speakers: null, found_speakers: 2 },
        slides: [
          {
            index: 1,
            title: '第一段',
            bullets: [],
            detail: '',
            timestamp: 5,
            image_url: null,
            points: [
              { speaker: 'S2', display: '發言者 B', start: 5, text: '一致的重點' },
              { speaker: 'S1', display: '發言者 B', start: 9, text: '編號是甲、名稱卻是乙的重點' },
              { speaker: 'S9', display: '（未知發言者）', start: 12, text: '編號不存在的重點' },
            ],
          },
        ],
      }),
    );
    const { status, html } = await get(site, '/article/conflict');
    assert.equal(status, 200);
    const lis = elements(html, 'li', 'data-point');
    assert.deepEqual(
      lis.map((l) => chipNames(l.inner)),
      [['發言者 B'], ['（未知發言者）'], ['（未知發言者）']],
    );
    const kind = (inner: string) => /<span class="spk"[^>]*\bdata-kind="([^"]*)"/.exec(inner)?.[1];
    assert.deepEqual(
      lis.map((l) => kind(l.inner)),
      ['participant', 'unknown', 'unknown'],
    );
    // 一致的那一點借用出場名單裡那位（第二位）的形狀；對不上的不借，種類是 unknown（上面已驗）
    const shape = (inner: string) => /<span class="spk"[^>]*\bdata-shape="([^"]*)"/.exec(inner)?.[1];
    assert.equal(shape(lis[0]?.inner ?? ''), 'square');
    // 全是參與者：逐字稿上方不該有「其他聲音」的說明
    assert.equal(elements(html, 'div', 'data-legend').length, 1);
    assert.ok(!text(html).includes('只列在逐字稿裡'));
  });

  const speakerA = {
    id: 'S1',
    label: '發言者 A',
    name: '',
    role: '',
    display: '發言者 A',
    kind: 'participant',
    seconds: 1,
    turns: 1,
    basis: 'none',
    flags: [],
  };
  const withPoints = (points: unknown[]) =>
    videoDetail({
      speakers: [speakerA],
      diarization: { status: 'ok', warnings: [], requested_speakers: null, found_speakers: 1 },
      slides: [{ index: 1, title: '第一段', bullets: [], detail: '', timestamp: 5, image_url: null, points }],
    });

  it('很長、沒有空白的一段文字（雜湊、網址）：重點的文字會折行，不把整頁撐寬', async () => {
    stub.detail.set(
      'longtoken',
      withPoints([
        { speaker: 'S1', display: '發言者 A', start: 5, text: 'W'.repeat(150) },
        { speaker: 'S1', display: '發言者 A', start: 9, text: `https://example.com/${'a'.repeat(200)}` },
      ]),
    );
    const { status, html } = await get(site, '/article/longtoken');
    assert.equal(status, 200);
    const lis = elements(html, 'li', 'data-point');
    assert.equal(lis.length, 2);
    for (const li of lis) {
      const cls = /<span class="([^"]*\bmin-w-0\b[^"]*)"/.exec(li.inner)?.[1] ?? '';
      assert.ok(/(^|\s)wrap-anywhere(\s|$)/.test(cls), `重點的文字要能在任何位置折行：${cls}`);
    }
    // 光有 class 不夠：建置出來的樣式表裡要真的有這一條規則
    const sheetTag = [...html.matchAll(/<link\b[^>]*>/g)].map((m) => m[0]).find((t) => /\brel="stylesheet"/.test(t));
    const sheet = /\bhref="([^"]+)"/.exec(sheetTag ?? '')?.[1];
    assert.ok(sheet, '找不到樣式表');
    const css = (await get(site, sheet)).html;
    assert.ok(/\.wrap-anywhere\s*\{[^}]*overflow-wrap:\s*anywhere/.test(css), '樣式表裡沒有 wrap-anywhere');
  });

  it('重點的時間大得不合理（超過一天）：不顯示時間、不產生 &t= 連結；重點本身還在', async () => {
    stub.detail.set(
      'hugestart',
      withPoints([
        { speaker: 'S1', display: '發言者 A', start: 1e308, text: '時間亂寫的重點' },
        { speaker: 'S1', display: '發言者 A', start: 1e9, text: '時間大得離譜的重點' },
        { speaker: 'S1', display: '發言者 A', start: 90, text: '時間正常的重點' },
      ]),
    );
    const { status, html } = await get(site, '/article/hugestart');
    assert.equal(status, 200);
    const lis = elements(html, 'li', 'data-point');
    assert.equal(lis.length, 3);
    for (const [i, want] of ['時間亂寫的重點', '時間大得離譜的重點'].entries()) {
      const li = lis[i]?.inner ?? '';
      assert.ok(text(li).includes(want));
      assert.ok(!li.includes('href='), `${want}：不能有時間連結`);
      assert.ok(!/\d:\d\d/.test(text(li)), `${want}：不能顯示時間：${text(li)}`);
    }
    assert.ok(unescape(lis[2]?.inner ?? '').includes('&t=90s'));
    assert.ok(text(lis[2]?.inner ?? '').includes('01:30'));
    assert.ok(!/[?&]t=[\d.]*e/.test(unescape(html)), '連結裡不能出現指數寫法的秒數');
  });

  it('殘留的 [[Sn]]：每個會顯示的字串都清掉，換成（未知發言者）', async () => {
    stub.detail.set(
      'leaky',
      videoDetail({
        title: '標題 [[S1]]',
        teaser: '導言 [[S2]]：開頭，被截到一半 [[S',
        source_note: '說明 [[S0]]',
        speaker: '頻道[[S3]]',
        transcript_text: '00:05 [[S1]]：甲說\n00:09 發言者 B：乙說 [[S9]]\n00:30 [[S',
        speakers: [
          {
            id: 'S1',
            label: '發言者 A',
            name: '',
            role: '',
            display: '發言者 A',
            kind: 'participant',
            seconds: 1,
            turns: 1,
            basis: 'none',
            flags: [],
          },
          {
            id: 'S2',
            label: '發言者 B',
            name: '範例[[S4]]',
            role: '',
            display: '發言者 B',
            kind: 'participant',
            seconds: 1,
            turns: 1,
            basis: 'none',
            flags: [],
          },
        ],
        diarization: { status: 'ok', warnings: [], requested_speakers: null, found_speakers: 2 },
        slides: [
          {
            index: 1,
            title: '段落 [[S1]]',
            bullets: ['[[S1]]：條列'],
            detail: '[[S2]]：敘述[[S',
            timestamp: 5,
            image_url: null,
            points: [
              { speaker: 'S1', display: '[[S1]]', start: 5, text: '重點 [[S2]] 結尾' },
              { speaker: 'S9', display: '[[S9]]', start: 9, text: '不存在的說話者' },
            ],
          },
        ],
      }),
    );
    const { status, html } = await get(site, '/article/leaky');
    assert.equal(status, 200);
    assert.ok(!html.includes('[[S'), '頁面不能含 [[S');
    assert.ok(text(html).includes('（未知發言者）'));
  });

  it('身分依據亂寫（不是剛好 operator）：不能顯示成已由編輯確認', async () => {
    const mk = (id: string, basis: unknown) => ({
      id,
      label: `發言者 ${id}`,
      name: '',
      role: '',
      display: `發言者 ${id}`,
      kind: 'participant',
      seconds: 1,
      turns: 1,
      basis,
      flags: [],
    });
    stub.detail.set(
      'basis',
      videoDetail({
        speakers: [mk('S1', 'OPERATOR'), mk('S2', true), mk('S3', 1), mk('S4', undefined), mk('S5', 'confirmed')],
        diarization: { status: 'ok', warnings: [], requested_speakers: null, found_speakers: 5 },
      }),
    );
    const { html } = await get(site, '/article/basis');
    assert.ok(!text(html).includes('已由編輯確認'));
    assert.equal(count(text(html), '未確認身分'), 5);
  });

  it('診斷狀態看不懂、警告碼不認得：照樣顯示一句通用的警告，不吞掉', async () => {
    stub.detail.set(
      'weird-warn',
      videoDetail({
        speakers: [
          {
            id: 'S1',
            label: '發言者 A',
            name: '',
            role: '',
            display: '發言者 A',
            kind: 'participant',
            seconds: 1,
            turns: 1,
            basis: 'none',
            flags: [],
          },
        ],
        diarization: { status: 'degraded', warnings: ['from_the_future'], requested_speakers: null, found_speakers: 1 },
      }),
    );
    const { html } = await get(site, '/article/weird-warn');
    const banner = elements(html, 'section', 'data-diarization');
    assert.equal(attrOf(banner[0]?.open ?? '', 'data-diarization'), 'warn');
    assert.ok(text(html).includes('說話者的區分有不確定之處。'));
  });

  it('列表：卡片的導言、標題清掉殘留的佔位符；人數欄位亂寫就不顯示', async () => {
    const card = (slug: string, over: Record<string, unknown>) => ({
      slug,
      ivod_id: `yt-${slug}`,
      source: 'youtube',
      party: '',
      title: `影片 ${slug}`,
      speaker: '假頻道',
      meeting: '',
      date: '2026-11-01',
      duration_seconds: 600,
      ivod_url: 'https://youtube.example.invalid/watch?v=X',
      teaser: '導言',
      cover_image_url: null,
      slide_count: 1,
      ...over,
    });
    stub.list.value = {
      count: 7,
      page: 1,
      page_size: 12,
      pages: 1,
      items: [
        card('a', { participant_count: 3, teaser: '[[S1]]：被漏掉的佔位符 [[S' }),
        card('b', { participant_count: '3' }),
        card('c', { participant_count: -1 }),
        card('d', { participant_count: 2.5 }),
        card('e', {}),
        card('f', { participant_count: 25 }),
        card('g', { participant_count: 1e21 }),
      ],
    };
    const { status, html } = await get(site, '/videos');
    assert.equal(status, 200);
    assert.ok(!html.includes('[[S'));
    const cards = [...html.matchAll(/<article\b[\s\S]*?<\/article>/g)].map((m) => text(m[0]));
    assert.equal(cards.length, 7);
    assert.ok(cards[0]?.includes('3 位說話者'));
    for (const c of cards.slice(1)) assert.ok(!c.includes('位說話者'), c);
  });

  it('列表是空的：指令不再要求 --speakers 1（預設就是多人）', async () => {
    stub.list.value = { count: 0, page: 1, page_size: 12, pages: 1, items: [] };
    const { status, html } = await get(site, '/videos');
    assert.equal(status, 200);
    const t = text(html);
    assert.ok(t.includes('還沒有加入任何影片'));
    assert.ok(t.includes("manage.py add_video '<影片網址>'"));
    assert.ok(!t.includes('--speakers 1 是聲明'), '舊的「必須加 --speakers 1」說明要拿掉');
    assert.ok(!/add_video '<影片網址>' --speakers 1/.test(t), '範例指令不能再帶 --speakers 1');
  });
});
