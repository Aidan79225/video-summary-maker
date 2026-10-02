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
  /**
   * 沒有值的原因；'no_committee_data'＝有分類過的報導，但沒有他這個會期的委員會資料；
   * 'no_caucus'＝他那個會期沒有參加黨團（院內紀錄的黨團一致率與跨黨投票）
   */
  reason?: string;
};

/** 議題分布的一列：一個領域在這個會期的篇數與占比（只算主領域） */
export type TopicShare = {
  /** 領域代碼，也是 /api/articles 的 topic */
  key: string;
  label: string;
  count: number;
  /** 0～100 的浮點：篇數 ÷ 基礎報導篇數 */
  share: number;
  /** 網站的相對路徑：主領域是這個領域的那幾篇 */
  evidence_url: string;
};

/** 算出這份分布的分類器，以及它在人工標註集上的評估結果 */
export type TopicClassifier = {
  /** 模型＋提示詞版本，例如「qwen3.5:9b#topic-v1」 */
  name: string;
  /** 0～1：主領域跟人工標註相同的比例 */
  accuracy: number;
  /** 人工標註的篇數（評估的分母） */
  labeled: number;
  evaluated_at: string;
};

/**
 * 追問清單上一項要求的狀態（後端依日期算）。
 * - followed：期限前後再提了同一件事（到期後的觀察期內都算）
 * - not_followed：觀察期結束，沒有再提
 * - watching：已經到期，還在觀察期內，還沒找到再提
 * - pending：還沒到期，也還沒找到再提
 */
export type FollowupState = 'followed' | 'not_followed' | 'watching' | 'pending';

/** 追問清單裡指到的一篇報導（都有頁面：後端只給 READY 的） */
export type FollowupArticle = {
  slug: string;
  title: string;
  date: string;
};

/** 追問清單的一項：他在某篇報導裡提出、寫了期限的一項要求 */
export type FollowupAsk = {
  /** 提出這項要求的報導 */
  article: FollowupArticle;
  request: string;
  /** 期限的原文，例如「一個月內」 */
  deadline: string;
  /** 程式從發言日期換算出來的到期日（YYYY-MM-DD）；清單上的都換得出來 */
  due_date: string;
  state: FollowupState;
  /** 再提這件事的那篇報導；只有 followed 有 */
  followed_by: FollowupArticle | null;
  /** 那篇報導逐字稿裡的原句（已過落地檢查）；只有 followed 有 */
  quote: string;
};

/** 判斷「有沒有再提」的模型，以及它在人工標註集上的評估結果 */
export type FollowupJudge = {
  /** 模型＋提示詞版本＋提示指紋，例如「qwen3.5:9b#followup-v1#…」 */
  name: string;
  /** 0～1：判斷跟人工標註相同的比例 */
  accuracy: number;
  /** 人工標註的對數（評估的分母） */
  labeled: number;
  evaluated_at: string;
};

export type ProfileBlock = {
  key: string;
  title: string;
  indicators: ProfileIndicator[];
  /**
   * 只有議題分布（key「topics」）有：12 個領域都列，依篇數由多到少。
   * 頁面把 0 篇的收成一行，不畫空長條。
   */
  distribution?: TopicShare[];
  /** 只有議題分布有；沒有通過評估的來源整個區塊都不會出現 */
  classifier?: TopicClassifier;
  /**
   * 只有追問（key「followup」）有：這個會期他提出、期限換算得出來的要求，依到期日排序。
   * 判斷器沒通過評估時只剩 pending——其他三種狀態要靠模型判斷。
   */
  asks?: FollowupAsk[];
  /** 只有追問有：期限寫法換算不出日期、因此不列入的要求項數 */
  unparsed?: number;
  /**
   * 只有追問有：通過評估的判斷器；null 是還沒有通過的，這時 indicators 是空的
   * （不給追問率），清單只有待追蹤
   */
  judge?: FollowupJudge | null;
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

/* ------------------------------------------------------------------
   院內紀錄（GET /api/people/{id}/records?session=&kind=）

   側寫「院內紀錄」區塊每個數字的證據：那個會期、那一類的每一筆紀錄，
   筆數等於指標的 n（或值）。只有立法院有。
   ------------------------------------------------------------------ */

/**
 * 紀錄的類別，也是網址的 kind；每一類對到哪個指標見 lib/records.ts 的 RECORD_KINDS。
 * 'caucus' 是一致率的分母（他有投票、黨團也有多數的表決）：設計列了七類，但一致率的 n
 * 跟投票出席率的 n 不一樣，少了這一類，一致率就點不回「n 筆」（後端也多了這一類）
 */
export type RecordKind =
  | 'plenary'
  | 'committee'
  | 'proposed'
  | 'cosigned'
  | 'passed'
  | 'votes'
  | 'caucus'
  | 'defections';

/*
 * 下面三種是網站整理後的形狀。後端（RecordOut）把代碼、編號放在 id，名稱、議題放在
 * title，票是中文（贊成／反對／棄權）；lib/records.ts 的 normalizeRecords 負責轉過來。
 */

/** 一場會議（院會或委員會），以及出席名單上有沒有他 */
export type MeetingRecord = {
  /** 會議代碼，例如「院會-11-5-23」 */
  code: string;
  /** 會議的第一天（YYYY-MM-DD） */
  date: string;
  name: string;
  /** 會議單位：院會，或委員會名稱；聯席會議是全部單位，用頓號分隔 */
  unit: string;
  attended: boolean;
  /** 官方連結（立法院議事暨公報資訊網）；沒有就是空字串 */
  url: string;
};

/** 一件委員提案 */
export type BillRecord = {
  bill_no: string;
  /** 提案日（YYYY-MM-DD）；後端沒給是空字串 */
  date: string;
  name: string;
  /** 議案狀態原文，例如「三讀」「交付審查」 */
  status: string;
  proposers: string[];
  url: string;
};

/** 一張票：贊成、反對、棄權 */
export type Ballot = 'yes' | 'no' | 'abstain';

/** 一次記名表決：他的票，以及他所屬黨團多數投的選項 */
export type VoteRecord = {
  /** 表決代碼 */
  code: string;
  meeting_code: string;
  /** 表決的日期（YYYY-MM-DD）；後端沒給是空字串，頁面改用 voted_at */
  date: string;
  /** 表決時間原文（LYAPI 的「表決時間」） */
  voted_at: string;
  /** 表決議題 */
  topic: string;
  /** 全院的票數；後端沒給是 null */
  tally: { yes: number; no: number; abstain: number } | null;
  /** 他的票；沒有投票是 null */
  vote: Ballot | null;
  /** 他那個會期的黨團；沒有參加黨團是空字串 */
  caucus: string;
  /** 黨團多數；並列（沒有多數）或沒有黨團時是 null，這一次不算進一致率與跨黨投票 */
  caucus_majority: Ballot | null;
  url: string;
};

/**
 * 一頁紀錄清單；items 的形狀跟著 kind 走。
 * dropped 是讀不懂、沒畫出來的筆數：清單筆數要等於指標的 n，少了幾筆要照實說，
 * 不能讓讀者以為數字錯了
 */
export type RecordList = { dropped: number } & (
  | { kind: 'plenary' | 'committee'; items: MeetingRecord[] }
  | { kind: 'proposed' | 'cosigned' | 'passed'; items: BillRecord[] }
  | { kind: 'votes' | 'caucus' | 'defections'; items: VoteRecord[] }
);

export type RecordQuery = {
  session: number;
  kind: RecordKind;
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
  /**
   * 領域代碼：只要主領域是這個領域的文章（只算該來源評估通過的分類器分出來的）。
   * 側寫議題分布每一列的連結帶的就是它。「any」是哪個領域都可以、但要分過類——
   * 聚焦度、廣度、委員會職掌內的比例的「看這 N 篇」帶的是它
   */
  topic?: string;
  page?: number;
  page_size?: number;
};
