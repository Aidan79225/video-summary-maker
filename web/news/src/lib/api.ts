import type {
  ApiError,
  ArticleDetail,
  ArticleList,
  ArticleQuery,
  Brief,
  Health,
  LawSource,
  Result,
  SpeakerList,
  Speaker,
  Party,
  PartyList,
  Profile,
  ProfileBlock,
  ProfileIndicator,
  ProfileQuery,
  ProfileSession,
} from './types';
import { toSource } from './sources';
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

/**
 * 站內連結只接受「/」開頭的相對路徑。
 *
 * 側寫的 evidence_url 由後端組好、直接變成可點的連結；跟 safeExternalUrl
 * 同一個理由，上游給了 javascript: 或 //別的網域（協定相對網址會跳出本站）
 * 都不能原樣放進 href。反斜線也擋：有些瀏覽器把 /\ 當成 //。
 */
export function safeInternalPath(path: string | null | undefined): string | null {
  if (!path) return null;
  const p = path.trim();
  return p.startsWith('/') && !p.startsWith('//') && !p.includes('\\') ? p : null;
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

/**
 * 假資料的文章多一個 session_id：真的 API 不把會期放在文章卡片上（篩選在
 * 後端做），假資料沒有後端，只好把「這篇掛在哪個會期」直接寫在文章上。
 */
type FixtureArticle = ArticleDetail & { session_id?: number | null };

/** 名冊：同來源、同名對到哪個人。對不到的名字就沒有 person_id，跟真的後端一樣 */
type FixturePerson = { id: number; name: string; source: string };

/**
 * 一個人在一個會期的側寫。evidence_url 不寫在檔案裡，由 fixtureProfile 照
 * 後端的規則組出來——手寫百分號編碼的中文名字太容易打錯，而且一錯就是死連結。
 */
type FixtureProfile = {
  person_id: number;
  source: string;
  session_id: number;
  computed_at: string;
  blocks: {
    key: string;
    title: string;
    indicators: Omit<ProfileIndicator, 'evidence_url'>[];
  }[];
};

const fx = fixture as unknown as {
  articles: FixtureArticle[];
  people?: FixturePerson[];
  sessions?: ProfileSession[];
  profiles?: FixtureProfile[];
};

function fixtureArticles(): FixtureArticle[] {
  return [...fx.articles].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
}

function toCard(a: FixtureArticle) {
  const { source_note, transcript_text, slides, brief, session_id, ...card } = a;
  void source_note;
  void transcript_text;
  void slides;
  void brief;
  void session_id;
  return card;
}

/** 聯合質詢的講者欄位是「甲、乙」；跟後端的 _joined_q 一樣逐一比對，不用 includes（「王立」會誤中「王立任」） */
function speakerNames(speaker: string): string[] {
  return speaker
    .split('、')
    .map((s) => s.trim())
    .filter(Boolean);
}

function fixturePersonId(name: string, source: string): number | null {
  return (fx.people ?? []).find((p) => p.name === name && p.source === source)?.id ?? null;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

/** 條文來源少了原文或官方連結就不顯示：一個沒有內容的連結只會讓人困惑 */
function normalizeSources(raw: unknown): LawSource[] {
  return (Array.isArray(raw) ? raw : [])
    .filter((x) => x && typeof x === 'object')
    .map((x) => ({
      law: str(x.law),
      article: str(x.article),
      title: str(x.title),
      excerpt: str(x.excerpt),
      official_url: str(x.official_url),
      api_url: str(x.api_url),
    }))
    .filter((x) => x.excerpt && safeExternalUrl(x.official_url));
}

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
      .map((n) => ({
        value: str(n.value),
        unit: str(n.unit),
        label: str(n.label),
        quote: str(n.quote),
        law: str(n.law),
        article: str(n.article),
        sources: normalizeSources(n.sources),
      }))
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
    if (query.speaker && !speakerNames(a.speaker).includes(query.speaker)) return false;
    if (query.source && a.source !== query.source) return false;
    // 下面三個跟後端的篩選同一套定義：側寫的「看這 N 篇」靠它們把 N 篇列出來
    if (query.session && a.session_id !== query.session) return false;
    // 單獨發言照 spec 的字面定義：講者欄位沒有「、」。不數 speakerNames 的人數——
    // 「甲、」這種殘缺的欄位後端算聯合質詢，這裡也要算，假資料的 count 才會等於 n
    if (query.solo && a.speaker.includes('、')) return false;
    if (query.has_brief && a.brief == null) return false;
    if (query.party && !(a.party ?? '').split('、').includes(query.party)) return false;
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
   人物側寫
   ------------------------------------------------------------------ */

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * 同儕少於這個人數就不給百分位（spec 的 MIN_PEERS）。API 沒有回這個值
 * （只回 min_sample），所以寫在這裡；後端改門檻時這裡要跟著改。
 */
const MIN_PEERS = 5;

function normalizeSession(raw: unknown): ProfileSession | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const id = num(o.id);
  const name = str(o.name).trim();
  if (id === null || !name) return null;
  return {
    id,
    source: toSource(str(o.source)),
    term: str(o.term),
    name,
    start_date: str(o.start_date) || null,
    end_date: str(o.end_date) || null,
  };
}

function normalizeIndicator(raw: unknown): ProfileIndicator | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const key = str(o.key);
  const label = str(o.label).trim();
  if (!key || !label) return null;
  const sampleOk = o.sample_ok === true;
  const value = num(o.value);
  const peers = Math.max(0, num(o.peers) ?? 0);
  const percentile = num(o.percentile);
  // 樣本不足、同儕不足、沒有值，都不給百分位。後端本來就回 null，這裡再擋一次：
  // 「最小樣本」與「同儕至少幾人」是側寫的底線，不該只靠一邊守。
  const comparable = sampleOk && value !== null && peers >= MIN_PEERS && percentile !== null;
  return {
    key,
    label,
    unit: str(o.unit),
    value,
    n: Math.max(0, num(o.n) ?? 0),
    n_unit: str(o.n_unit) || '篇',
    percentile: comparable ? Math.min(100, Math.max(0, percentile)) : null,
    peers,
    sample_ok: sampleOk,
    evidence_url: str(o.evidence_url),
  };
}

/**
 * 側寫缺欄位時不讓整頁爆掉：壞掉的指標丟掉、沒有任何指標就當作沒有側寫
 * （回 null，頁面整段不顯示），而不是畫出一個空殼。
 */
export function normalizeProfile(raw: unknown): Profile | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const person = (o.person && typeof o.person === 'object' ? o.person : {}) as Record<string, unknown>;
  const personId = num(person.id);
  const session = normalizeSession(o.session);
  if (personId === null || !session) return null;

  const blocks: ProfileBlock[] = (Array.isArray(o.blocks) ? o.blocks : [])
    .filter((b) => b && typeof b === 'object')
    .map((b) => ({
      key: str(b.key),
      title: str(b.title).trim(),
      indicators: (Array.isArray(b.indicators) ? (b.indicators as unknown[]) : [])
        .map(normalizeIndicator)
        .filter((i): i is ProfileIndicator => i !== null),
    }))
    .filter((b) => b.key && b.title && b.indicators.length > 0);
  if (blocks.length === 0) return null;

  const sessions = (Array.isArray(o.sessions) ? o.sessions : [])
    .map(normalizeSession)
    .filter((s): s is ProfileSession => s !== null);
  // 切換列至少要有目前這個會期，否則訪客看不出現在看的是哪一個
  if (!sessions.some((s) => s.id === session.id)) sessions.unshift(session);

  return {
    person: { id: personId, name: str(person.name) },
    source: toSource(str(o.source) || session.source),
    session,
    sessions,
    computed_at: str(o.computed_at),
    min_sample: num(o.min_sample) ?? 5,
    blocks,
  };
}

/**
 * 假資料的側寫：照 API 的規則挑來源與會期、組 evidence_url。
 *
 * 規則跟後端相同——指定了會期就用那個會期的來源；沒指定來源就用最近一個有統計的
 * 會期的來源；沒指定會期就用他有發言的最近一個會期，都沒有就用最近一個。這樣不連
 * Pi 也能看到切換會期、樣本不足、同儕不足這幾種版面。
 */
function fixtureProfile(personId: number, query: ProfileQuery): Result<unknown> {
  const notFound: Result<unknown> = {
    ok: false,
    error: { kind: 'notfound', status: 404, message: '假資料裡沒有這個人的側寫' },
  };
  const sessionOf = (id: number) => (fx.sessions ?? []).find((s) => s.id === id) ?? null;
  // 新舊跟後端的 _recency 一樣：先看資料涵蓋到哪一天，同一天再看起始日、id
  // （日期是 YYYY-MM-DD，直接比字串就是比先後；沒有日期當成最舊）
  const cmp = (x: string | number, y: string | number) => (x < y ? -1 : x > y ? 1 : 0);
  const newerFirst = (a: ProfileSession, b: ProfileSession) =>
    cmp(b.end_date ?? '', a.end_date ?? '') ||
    cmp(b.start_date ?? '', a.start_date ?? '') ||
    cmp(b.id, a.id);
  const mine = (fx.profiles ?? [])
    .filter((p) => p.person_id === personId)
    .flatMap((p) => {
      const s = sessionOf(p.session_id);
      return s ? [{ p, s }] : [];
    })
    .sort((a, b) => newerFirst(a.s, b.s));

  // 指定了會期：來源跟著會期走；同時指定了來源而對不上就是 404（後端同樣處理）
  let picked = query.session ? mine.find((x) => x.s.id === query.session) : undefined;
  if (query.session && (!picked || (query.source && picked.p.source !== query.source))) return notFound;
  const source = picked?.p.source || query.source || mine[0]?.p.source;
  const inSource = mine.filter((x) => x.p.source === source);
  const spoke = (x: (typeof inSource)[number]) =>
    x.p.blocks.some((b) => b.indicators.some((i) => i.key === 'speeches' && (i.value ?? 0) > 0));
  picked ??= inSource.find(spoke) ?? inSource[0];
  if (!picked || !source) return notFound;

  // 證據連結用這個人在該來源的寫法，跟後端一樣
  const name = (fx.people ?? []).find((p) => p.id === personId && p.source === source)?.name ?? '';
  const evidence = `/speaker/${encodeURIComponent(name)}?source=${source}&session=${picked.s.id}`;
  return {
    ok: true,
    data: {
      person: { id: personId, name },
      source,
      session: picked.s,
      sessions: inSource.map((x) => x.s),
      computed_at: picked.p.computed_at,
      min_sample: 5,
      blocks: picked.p.blocks.map((b) => ({
        ...b,
        indicators: b.indicators.map((i) => ({
          ...i,
          // 具體度只算單獨發言、有摘要卡的文章，證據清單也要用同一組篩選
          evidence_url: b.key === 'specificity' ? `${evidence}&solo=1&brief=1` : evidence,
        })),
      })),
    },
  };
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
  if (query.source) params.set('source', query.source);
  if (query.party) params.set('party', query.party);
  if (query.q) params.set('q', query.q);
  if (query.session) params.set('session', String(query.session));
  if (query.solo) params.set('solo', 'true');
  if (query.has_brief) params.set('has_brief', 'true');
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
    return { ok: true, data: { ...found, brief: normalizeBrief(found.brief) } };
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
    },
  };
}

export async function getParties(source?: string): Promise<Result<PartyList>> {
  if (isFixtureMode()) {
    const map = new Map<string, Party>();
    for (const a of fixtureArticles()) {
      if (source && a.source !== source) continue;
      for (const name of (a.party ?? '').split('、').map((s) => s.trim()).filter(Boolean)) {
        const cur = map.get(name);
        if (cur) {
          cur.count += 1;
          if (!cur.latest_date || a.date > cur.latest_date) cur.latest_date = a.date;
        } else {
          map.set(name, { name, count: 1, latest_date: a.date });
        }
      }
    }
    return { ok: true, data: { items: [...map.values()].sort((a, b) => b.count - a.count) } };
  }
  const res = await getJson<PartyList>(source ? `/api/parties?source=${source}` : '/api/parties');
  if (!res.ok) return res;
  return { ok: true, data: { items: Array.isArray(res.data?.items) ? res.data.items : [] } };
}

export async function getSpeakers(source?: string): Promise<Result<SpeakerList>> {
  if (isFixtureMode()) {
    const map = new Map<string, Speaker>();
    for (const a of fixtureArticles()) {
      if (source && a.source !== source) continue;
      // 跟後端一樣把聯合質詢拆開、每人各算一篇，發言者頁才找得到每一位的 person_id
      for (const name of speakerNames(a.speaker)) {
        const key = `${a.source}:${name}`;
        const cur = map.get(key);
        if (cur) {
          cur.count += 1;
          if (a.date > cur.latest_date) cur.latest_date = a.date;
        } else {
          map.set(key, {
            name,
            source: a.source,
            count: 1,
            latest_date: a.date,
            person_id: fixturePersonId(name, a.source),
          });
        }
      }
    }
    return { ok: true, data: { items: [...map.values()].sort((a, b) => b.count - a.count) } };
  }

  const res = await getJson<SpeakerList>(source ? `/api/speakers?source=${source}` : '/api/speakers');
  if (!res.ok) return res;
  return { ok: true, data: { items: Array.isArray(res.data?.items) ? res.data.items : [] } };
}

/**
 * 人物側寫。source／session 可省略，由後端挑預設（見 spec 的 API 一節）。
 *
 * 呼叫端的約定：失敗（含 404：這個人在這個來源還沒有統計）就整段不顯示，
 * 發言者頁其餘部分照常——側寫是附加的，不能拖垮報導清單。
 */
export async function getProfile(personId: number, query: ProfileQuery = {}): Promise<Result<Profile>> {
  // person_id 來自後端，但它會被拼進路徑；不是正整數就不送出去
  if (!Number.isSafeInteger(personId) || personId <= 0) {
    return { ok: false, error: { kind: 'notfound', status: 404, message: '沒有這個人' } };
  }

  let res: Result<unknown>;
  if (isFixtureMode()) {
    res = fixtureProfile(personId, query);
  } else {
    const params = new URLSearchParams();
    if (query.source) params.set('source', query.source);
    if (query.session) params.set('session', String(query.session));
    const qs = params.toString();
    res = await getJson<unknown>(`/api/people/${personId}/profile${qs ? `?${qs}` : ''}`);
  }
  if (!res.ok) return res;

  const profile = normalizeProfile(res.data);
  if (!profile) return { ok: false, error: { kind: 'parse', message: '側寫資料不完整' } };
  return { ok: true, data: profile };
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
