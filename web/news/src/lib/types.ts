import type { ArticleSource } from './sources';

export type { ArticleSource };

export type ArticleCard = {
  slug: string;
  /** 來源給的識別碼；立法院是 IVOD 數字、臺中市議會是 tccc-<ano>、新北市議會是 ntpc-<檔案key> */
  ivod_id: string;
  source: ArticleSource;
  /** 講者「當時」的政黨全名；聯合質詢多黨用頓號分隔；查無資料是空字串 */
  party: string;
  title: string;
  speaker: string;
  meeting: string;
  date: string;
  duration_seconds: number;
  ivod_url: string;
  teaser: string;
  cover_image_url: string | null;
  slide_count: number;
};

export type Slide = {
  index: number;
  title: string;
  bullets: string[];
  detail: string;
  timestamp: number;
  image_url: string | null;
};

/** 一條法條的原文，附官方與 LYAPI 兩個連結。只附來源，不判對錯。 */
export type LawSource = {
  law: string;
  article: string;
  title: string;
  excerpt: string;
  official_url: string;
  api_url: string;
};

export type KeyNumber = {
  /** 只有阿拉伯數字，例如 "82.4"；單位另放，卡片才能把數字放大、單位縮小 */
  value: string;
  unit: string;
  label: string;
  /** 逐字稿裡講出這個數字的那句話，生成端已驗證過它真的在逐字稿裡 */
  quote: string;
  /** 講者自己講出的法律名稱與條號（「第106條」）；不是在講法條就都是空字串 */
  law: string;
  article: string;
  /** 後端抓回來的條文原文：講者引的那一條，加上提到它的罰則條文 */
  sources: LawSource[];
};

export type Ask = {
  request: string;
  deadline: string;
  response: string;
};

/** 摘要卡：一句話、關鍵數字、要求與回應。後端產不出來時整個是 null。 */
export type Brief = {
  one_liner: string;
  key_numbers: KeyNumber[];
  asks: Ask[];
};

export type ArticleDetail = ArticleCard & {
  source_note: string;
  transcript_text: string;
  brief: Brief | null;
  slides: Slide[];
};

export type ArticleList = {
  count: number;
  page: number;
  page_size: number;
  pages: number;
  items: ArticleCard[];
};

export type Speaker = {
  name: string;
  source: ArticleSource;
  count: number;
  latest_date: string;
  party?: string;
  district?: string;
  /**
   * 同來源、同名的任期所屬的人（Person）。發言者頁靠它找側寫；
   * 名冊對不到（或舊後端沒給）就是 null／缺欄位，頁面就不顯示側寫。
   */
  person_id?: number | null;
};

/* ------------------------------------------------------------------
   人物側寫（GET /api/people/{id}/profile）

   數字全部由後端算好：前端只排版，不重算、不加總、不排名。
   ------------------------------------------------------------------ */

/**
 * 會期。start_date／end_date 是「資料涵蓋範圍」（掛在這個會期的文章最早與
 * 最晚的日期），不是官方起訖；頁面要寫「資料涵蓋」，不能寫成會期起訖。
 */
export type ProfileSession = {
  id: number;
  source: ArticleSource;
  term: string;
  name: string;
  start_date: string | null;
  end_date: string | null;
};

export type ProfileIndicator = {
  key: string;
  label: string;
  unit: string;
  /** n 為 0 時是 null（沒有分母就沒有值） */
  value: number | null;
  n: number;
  /** n 的單位：「篇」或「個數字」 */
  n_unit: string;
  /** 0～100 的浮點；樣本不足或同儕不足時是 null */
  percentile: number | null;
  /** 同儕人數（百分位的母體） */
  peers: number;
  /** n 達到最小樣本；false 時只顯示原始計數 */
  sample_ok: boolean;
  /** 網站的相對路徑，點進去就是算這個數字用的那幾篇 */
  evidence_url: string;
};

export type ProfileBlock = {
  key: string;
  title: string;
  indicators: ProfileIndicator[];
};

export type Profile = {
  person: { id: number; name: string };
  source: ArticleSource;
  session: ProfileSession;
  /** 同來源、他有統計的所有會期，新的在前 */
  sessions: ProfileSession[];
  computed_at: string;
  min_sample: number;
  blocks: ProfileBlock[];
};

export type ProfileQuery = {
  source?: string;
  session?: number;
};

export type Party = {
  name: string;
  count: number;
  latest_date: string | null;
};

export type PartyList = { items: Party[] };

export type SpeakerList = { items: Speaker[] };

export type Health = {
  ok: boolean;
  articles: number;
  latest_date: string | null;
};

export type ApiErrorKind =
  | 'offline'      // 連不上（Django 還沒起來 / Pi 剛開機）
  | 'timeout'      // 逾時
  | 'server'       // 5xx
  | 'notfound'     // 404
  | 'client'       // 其他 4xx
  | 'parse';       // 回應不是預期的 JSON

export type ApiError = {
  kind: ApiErrorKind;
  message: string;
  status?: number;
  detail?: string;
};

export type Result<T> = { ok: true; data: T } | { ok: false; error: ApiError };

export type ArticleQuery = {
  date?: string;
  speaker?: string;
  source?: string;
  party?: string;
  q?: string;
  /** 會期 id：只要掛在這個會期的文章 */
  session?: number;
  /** 只要單獨發言（講者欄位沒有「、」）；搭配 speaker 就是講者完全等於這個名字 */
  solo?: boolean;
  /** 只要有摘要卡的文章 */
  has_brief?: boolean;
  page?: number;
  page_size?: number;
};
