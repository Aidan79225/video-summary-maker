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

export type ArticleDetail = ArticleCard & {
  source_note: string;
  transcript_text: string;
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
  count: number;
  latest_date: string;
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
