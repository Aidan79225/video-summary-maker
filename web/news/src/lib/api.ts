import type {
  ApiError,
  ArticleDetail,
  ArticleList,
  ArticleQuery,
  Brief,
  Health,
  Result,
  SpeakerClaim,
  SpeakerClaims,
  SpeakerList,
} from './types';
import { normalizeClaims, normalizeScore, scoreOf } from './factcheck';
import fixture from '../fixtures/sample.json';

/* ------------------------------------------------------------------
   環境變數

   process.env 優先於 import.meta.env，這樣正式站（node entry.mjs）只要
   改 systemd 的 Environment= 就會生效，不必重新 build。
   ------------------------------------------------------------------ */

const BUILD_API_BASE = import.meta.env.PUBLIC_API_BASE as string | undefined;
const BUILD_USE_FIXTURE = import.meta.env.USE_FIXTURE as string | undefined;
const BUILD_MEDIA_BASE = import.meta.env.PUBLIC_MEDIA_BASE as string | undefined;

function runtimeEnv(key: string): string | undefined {
  try {
    const v = typeof process !== 'undefined' && process.env ? process.env[key] : undefined;
    return v === '' ? undefined : v;
  } catch {
    return undefined;
  }
}

export function apiBase(): string {
  const raw = runtimeEnv('PUBLIC_API_BASE') ?? BUILD_API_BASE ?? 'http://localhost:8000';
  return raw.replace(/\/+$/, '');
}

export function isFixtureMode(): boolean {
  const v = runtimeEnv('USE_FIXTURE') ?? BUILD_USE_FIXTURE ?? '0';
  return v === '1' || v.toLowerCase() === 'true';
}

function timeoutMs(): number {
  const n = Number(runtimeEnv('API_TIMEOUT_MS') ?? 8000);
  return Number.isFinite(n) && n > 0 ? n : 8000;
}

/**
 * 圖片的 base 與 API 的 base 分開。
 *
 * API base 是 SSR 在伺服器端用的，可以是 127.0.0.1；但圖片網址會原樣送到
 * 瀏覽器，用 127.0.0.1 的話，任何用手機開這個站的人都會看到一排破圖。
 * 沒設就沿用 API base（單機開發時兩者本來就相同）。
 */
export function mediaBase(): string {
  const raw = runtimeEnv('PUBLIC_MEDIA_BASE') ?? BUILD_MEDIA_BASE;
  return raw ? raw.replace(/\/+$/, '') : apiBase();
}

/** 相對路徑（/media/...）接上圖片 base；已經是絕對網址就原樣回傳 */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  if (/^https?:\/\//i.test(path)) return path;
  return `${mediaBase()}${path.startsWith('/') ? '' : '/'}${path}`;
}

/**
 * 外部連結只接受 http(s)。
 *
 * ivod_url 來自立法院的 API，而它會直接變成頁面上可點的連結——上游哪天
 * 回了 javascript: 就是一個點擊型 XSS，Astro 的屬性跳脫擋不住這件事。
 */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  return /^https?:\/\//i.test(url.trim()) ? url.trim() : null;
}

/* ------------------------------------------------------------------
   錯誤訊息（給人看的，不是 stack trace）
   ------------------------------------------------------------------ */

export function errorTitle(e: ApiError): string {
  switch (e.kind) {
    case 'offline':
      return '連不上內容伺服器';
    case 'timeout':
      return '內容伺服器沒有回應';
    case 'server':
      return '內容伺服器發生錯誤';
    case 'notfound':
      return '找不到這篇報導';
    case 'client':
      return '這個請求無法處理';
    default:
      return '收到無法解讀的回應';
  }
}

export function errorHint(e: ApiError): string {
  // 訊息裡不放 API base：那是內部位址，而這段字是訪客看得到的。
  // 要查是哪一台連不上，看伺服器的 log。
  switch (e.kind) {
    case 'offline':
      return '內容伺服器目前沒有回應。若這台機器剛重新開機，後端可能還在啟動中，稍候重新整理即可。';
    case 'timeout':
      return `等待內容伺服器超過 ${Math.round(timeoutMs() / 1000)} 秒仍無回應。伺服器可能正在忙，請稍後再試。`;
    case 'server':
      return `後端回報 HTTP ${e.status ?? 500}。這是內容伺服器那一側的問題，網站本身是正常的。`;
    case 'notfound':
      return '這個連結指向的內容不存在，可能已被移除，或是網址有誤。';
    case 'client':
      return `後端回報 HTTP ${e.status ?? 400}。請檢查篩選條件是否正確。`;
    default:
      return '後端回應的格式和預期不符，可能是 API 版本不一致。';
  }
}

function toApiError(err: unknown): ApiError {
  const msg = err instanceof Error ? err.message : String(err);
  const name = err instanceof Error ? err.name : '';
  if (name === 'TimeoutError' || name === 'AbortError' || /timeout|abort/i.test(msg)) {
    return { kind: 'timeout', message: '請求逾時', detail: msg };
  }
  return { kind: 'offline', message: '無法連線到 API', detail: msg };
}

/* ------------------------------------------------------------------
   HTTP
   ------------------------------------------------------------------ */

async function getJson<T>(path: string): Promise<Result<T>> {
  const url = `${apiBase()}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(timeoutMs()),
    });
  } catch (err) {
    return { ok: false, error: toApiError(err) };
  }

  if (!res.ok) {
    // 只有 422（django-ninja 的參數驗證）才是訪客送錯東西。其餘 4xx——
    // ALLOWED_HOSTS 沒設對是 400、反向代理的 401/403/429——都是這一端的
    // 設定問題，當成訪客的錯會讓「網站整個掛了」顯示成「請檢查篩選條件」。
    const kind: ApiError['kind'] =
      res.status === 404 ? 'notfound' : res.status === 422 ? 'client' : 'server';
    return {
      ok: false,
      // detail 只放路徑：完整網址含內部位址，而它會顯示在訪客看得到的
      // 「技術細節」裡。
      error: { kind, status: res.status, message: `HTTP ${res.status}`, detail: path },
    };
  }

  try {
    return { ok: true, data: (await res.json()) as T };
  } catch (err) {
    return {
      ok: false,
      error: {
        kind: 'parse',
        message: '回應不是合法的 JSON',
        detail: err instanceof Error ? err.message : String(err),
      },
    };
  }
}

/* ------------------------------------------------------------------
   假資料模式（USE_FIXTURE=1）
   ------------------------------------------------------------------ */

const fx = fixture as unknown as { articles: ArticleDetail[] };

function fixtureArticles(): ArticleDetail[] {
  return [...fx.articles].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
}

function toCard(a: ArticleDetail) {
  const { source_note, transcript_text, slides, brief, claims, factcheck_checked, ...card } = a;
  void source_note;
  void transcript_text;
  void slides;
  void brief;
  void claims;
  void factcheck_checked;
  return card;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/**
 * 摘要卡缺欄位不讓整頁爆掉；沒有一句話就當作沒有卡片，
 * 版面退回只用導言的樣子。
 */
export function normalizeBrief(raw: unknown): Brief | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const one_liner = str(o.one_liner).trim();
  if (!one_liner) return null;
  const list = (v: unknown) => (Array.isArray(v) ? v : []).filter((x) => x && typeof x === 'object');
  return {
    one_liner,
    key_numbers: list(o.key_numbers)
      .map((n) => ({ value: str(n.value), unit: str(n.unit), label: str(n.label), quote: str(n.quote) }))
      .filter((n) => n.value && n.label),
    asks: list(o.asks)
      .map((a) => ({ request: str(a.request), deadline: str(a.deadline), response: str(a.response) }))
      .filter((a) => a.request),
  };
}

function fixtureList(query: ArticleQuery): ArticleList {
  const page = Math.max(1, Number(query.page) || 1);
  const pageSize = Math.max(1, Number(query.page_size) || 12);
  const q = (query.q ?? '').trim();

  const filtered = fixtureArticles().filter((a) => {
    if (query.date && a.date !== query.date) return false;
    if (query.speaker && a.speaker !== query.speaker) return false;
    if (q) {
      const hay = `${a.title} ${a.teaser} ${a.meeting} ${a.speaker} ${a.transcript_text}`;
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const items = filtered.slice((page - 1) * pageSize, page * pageSize).map(toCard);
  return { count: filtered.length, page, page_size: pageSize, pages, items };
}

/* ------------------------------------------------------------------
   對外 API
   ------------------------------------------------------------------ */

export async function getHealth(): Promise<Result<Health>> {
  if (isFixtureMode()) {
    const all = fixtureArticles();
    return {
      ok: true,
      data: { ok: true, articles: all.length, latest_date: all[0]?.date ?? null },
    };
  }
  return getJson<Health>('/api/health');
}

export async function getArticles(query: ArticleQuery = {}): Promise<Result<ArticleList>> {
  if (isFixtureMode()) return { ok: true, data: fixtureList(query) };

  const params = new URLSearchParams();
  if (query.date) params.set('date', query.date);
  if (query.speaker) params.set('speaker', query.speaker);
  if (query.q) params.set('q', query.q);
  params.set('page', String(Math.max(1, Number(query.page) || 1)));
  params.set('page_size', String(Math.max(1, Number(query.page_size) || 12)));

  const res = await getJson<ArticleList>(`/api/articles?${params.toString()}`);
  if (!res.ok) return res;

  // 後端若少給欄位，補成可以安全 render 的形狀，避免整頁爆掉
  const d = (res.data ?? {}) as Partial<ArticleList>;
  return {
    ok: true,
    data: {
      count: Number(d.count) || 0,
      page: Number(d.page) || 1,
      page_size: Number(d.page_size) || 12,
      pages: Number(d.pages) || 1,
      items: Array.isArray(d.items) ? d.items : [],
    },
  };
}

export async function getArticle(slug: string): Promise<Result<ArticleDetail>> {
  if (isFixtureMode()) {
    const found = fixtureArticles().find((a) => a.slug === slug);
    if (!found) {
      return {
        ok: false,
        error: { kind: 'notfound', status: 404, message: '假資料裡沒有這一篇' },
      };
    }
    return {
      ok: true,
      data: {
        ...found,
        brief: normalizeBrief(found.brief),
        // 示範資料裡有 claims 欄位就視為查核過
        factcheck_checked: Array.isArray(found.claims),
        claims: normalizeClaims(found.claims),
      },
    };
  }

  const res = await getJson<ArticleDetail>(`/api/articles/${encodeURIComponent(slug)}`);
  if (!res.ok) return res;

  const d = res.data;
  if (!d || typeof d.slug !== 'string') {
    return { ok: false, error: { kind: 'parse', message: '文章資料不完整' } };
  }
  return {
    ok: true,
    data: {
      ...d,
      slides: Array.isArray(d.slides) ? d.slides : [],
      transcript_text: typeof d.transcript_text === 'string' ? d.transcript_text : '',
      brief: normalizeBrief(d.brief),
      factcheck_checked: d.factcheck_checked === true,
      claims: normalizeClaims(d.claims),
    },
  };
}

export async function getSpeakers(): Promise<Result<SpeakerList>> {
  if (isFixtureMode()) {
    const map = new Map<string, { name: string; count: number; latest_date: string; verdicts: string[] }>();
    for (const a of fixtureArticles()) {
      const verdicts = normalizeClaims(a.claims).map((c) => c.verdict);
      const cur = map.get(a.speaker);
      if (cur) {
        cur.count += 1;
        cur.verdicts.push(...verdicts);
        if (a.date > cur.latest_date) cur.latest_date = a.date;
      } else {
        map.set(a.speaker, { name: a.speaker, count: 1, latest_date: a.date, verdicts });
      }
    }
    const items = [...map.values()]
      .sort((a, b) => b.count - a.count)
      .map(({ verdicts, ...s }) => ({ ...s, factcheck: scoreOf(verdicts) }));
    return { ok: true, data: { items } };
  }

  const res = await getJson<SpeakerList>('/api/speakers');
  if (!res.ok) return res;
  const items = Array.isArray(res.data?.items) ? res.data.items : [];
  return {
    ok: true,
    data: { items: items.map((s) => ({ ...s, factcheck: normalizeScore(s.factcheck) })) },
  };
}

export async function getSpeakerClaims(name: string): Promise<Result<SpeakerClaims>> {
  if (isFixtureMode()) {
    const items: SpeakerClaim[] = [];
    for (const a of fixtureArticles().filter((x) => x.speaker === name)) {
      for (const c of normalizeClaims(a.claims)) {
        items.push({ ...c, article_slug: a.slug, article_title: a.title, date: a.date });
      }
    }
    return {
      ok: true,
      data: { speaker: name, score: scoreOf(items.map((c) => c.verdict)), items },
    };
  }

  const res = await getJson<SpeakerClaims>(`/api/speakers/${encodeURIComponent(name)}/claims`);
  if (!res.ok) return res;
  return {
    ok: true,
    data: {
      speaker: name,
      score: normalizeScore(res.data?.score),
      items: normalizeClaims<SpeakerClaim>(res.data?.items),
    },
  };
}

/**
 * 這一頁該回什麼 HTTP 狀態。
 *
 * 訪客打了 ?date=abc 是**訪客**的問題，不是後端掛了——全部回 503 的話，
 * 爬蟲會讓 Pi 上的監控看到一堆假的「後端掛了」。
 */
export function statusFor(kind: ApiError['kind']): number {
  if (kind === 'notfound') return 404;
  if (kind === 'client') return 400;
  return 503;
}
