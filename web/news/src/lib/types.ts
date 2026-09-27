export type ArticleCard = {
  slug: string;
  ivod_id: string;
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

export type KeyNumber = {
  /** 只有阿拉伯數字，例如 "82.4"；單位另放，卡片才能把數字放大、單位縮小 */
  value: string;
  unit: string;
  label: string;
  /** 逐字稿裡講出這個數字的那句話，生成端已驗證過它真的在逐字稿裡 */
  quote: string;
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

export type Verdict = 'supported' | 'partial' | 'contradicted' | 'unverifiable';

export type ClaimMethod = 'numeric' | 'model' | 'none';

export type Evidence = {
  source: string;
  title: string;
  official_url: string;
  api_url: string;
  excerpt: string;
};

export type Claim = {
  index: number;
  quote: string;
  timestamp: number;
  kind: string;
  statement: string;
  verdict: Verdict;
  method: ClaimMethod;
  rationale: string;
  /** 「不符」經人工核准後才會出現；true 代表有人看過 */
  reviewed: boolean;
  evidence: Evidence[];
};

export type FactCheckScore = {
  /** 可查證陳述不足 min_sample 則時為 null */
  rate: number | null;
  checked: number;
  supported: number;
  partial: number;
  contradicted: number;
  unverifiable: number;
  min_sample: number;
};

export type SpeakerClaim = Claim & {
  article_slug: string;
  article_title: string;
  date: string;
};

export type SpeakerClaims = {
  speaker: string;
  score: FactCheckScore;
  items: SpeakerClaim[];
};

export type ArticleDetail = ArticleCard & {
  source_note: string;
  transcript_text: string;
  brief: Brief | null;
  slides: Slide[];
  factcheck_checked: boolean;
  claims: Claim[];
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
  count: number;
  latest_date: string;
  factcheck?: FactCheckScore;
};

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
  q?: string;
  page?: number;
  page_size?: number;
};
