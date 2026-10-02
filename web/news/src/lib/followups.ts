/**
 * 追問率（側寫第三步）：固定的規則，以及 API 追問區塊的整理。
 *
 * 唯一的來源在後端（services/news/articles/followups.py）：狀態、到期日、追問率都由後端
 * 算好，網站只排版。這裡留一份規則，是因為方法頁要把期限換算表、四種狀態與門檻公開給
 * 讀者看；後端改了規則，這裡跟方法頁要一起改。
 *
 * 跟議題分布一樣，模型參與的部分「判斷器沒通過人工標註的評估就不上線」：後端擋過，
 * 這裡再擋一次（normalizeFollowupBlock），不讓一個格式錯的回應把沒驗證過的比率放上頁面。
 */
import type {
  FollowupArticle,
  FollowupAsk,
  FollowupJudge,
  FollowupState,
  ProfileBlock,
} from './types';

/** 側寫區塊的 key（後端的 FOLLOWUP_BLOCK） */
export const FOLLOWUP_BLOCK = 'followup';

/** 側寫頁上追問區塊的錨點：追問率的 evidence_url 指到這裡（清單就在側寫裡） */
export const FOLLOWUP_ANCHOR = 'followups';

/**
 * 判斷器通過評估的門檻（後端的 FOLLOWUP_MIN_LABELS、FOLLOWUP_MIN_ACCURACY）。
 * 比議題分類的 80% 高：判錯會把有追到底的人標成沒追。後端改門檻時這裡要跟著改。
 */
export const FOLLOWUP_MIN_LABELS = 30;
export const FOLLOWUP_MIN_ACCURACY = 0.85;

/** 到期後再看多久：這段時間內再提都算追問，過了還沒有就是未追問 */
export const FOLLOWUP_WATCH_DAYS = 90;

/** 程式篩選後最多送幾篇候選報導給模型判斷 */
export const FOLLOWUP_TOP_CANDIDATES = 3;

/** 落地檢查：引用去掉空白與標點後至少幾個字 */
export const FOLLOWUP_QUOTE_MIN_CHARS = 6;

/** 送給模型的那段逐字稿最多幾個字 */
export const FOLLOWUP_EXCERPT_MAX_CHARS = 1500;

export type FollowupStateInfo = {
  key: FollowupState;
  label: string;
  /** 一句話的定義：清單每一組的說明，方法頁的狀態表也用它 */
  description: string;
  /** 這個狀態要靠模型判斷才分得出來；判斷器沒通過時不顯示 */
  judged: boolean;
};

/**
 * 四種狀態，順序就是清單分組的順序：先列有結果的（已追問、未追問），再列還在等的。
 * 「待追蹤」不靠模型——還沒到期就是還沒到期——所以判斷器沒通過時只剩它。
 */
export const FOLLOWUP_STATES: readonly FollowupStateInfo[] = [
  {
    key: 'followed',
    label: '已追問',
    description: `提出之後、到期後 ${FOLLOWUP_WATCH_DAYS} 天內，他在另一次質詢再提了同一件事。`,
    judged: true,
  },
  {
    key: 'not_followed',
    label: '未追問',
    description: `到期後 ${FOLLOWUP_WATCH_DAYS} 天的觀察期結束，都沒有找到他再提這件事。`,
    judged: true,
  },
  {
    key: 'watching',
    label: '觀察中',
    description: `已經到期，還在 ${FOLLOWUP_WATCH_DAYS} 天的觀察期內，目前還沒找到他再提。`,
    judged: true,
  },
  {
    key: 'pending',
    label: '待追蹤',
    description: '還沒到期，也還沒找到他再提。',
    judged: false,
  },
];

const STATE_KEYS = new Set<string>(FOLLOWUP_STATES.map((s) => s.key));

export function isFollowupState(v: unknown): v is FollowupState {
  return typeof v === 'string' && STATE_KEYS.has(v);
}

/**
 * 期限換算表（後端 followups.py 的規則）。方法頁照這張表列；example 是給讀者看的算例，
 * 各自寫明發言日期，不共用一個日期——「本會期」「X 月底前」要不同的日期才看得出規則。
 */
export const DEADLINE_RULES: readonly { phrase: string; due: string; example: string }[] = [
  { phrase: 'N 天內、N 日內', due: '發言日 ＋ N 天', example: '8 月 27 日說「十天內」→ 9 月 6 日' },
  {
    phrase: 'N 週內、N 周內、N 星期內、N 個禮拜內',
    due: '發言日 ＋ 7 × N 天',
    example: '8 月 27 日說「兩週內」→ 9 月 10 日',
  },
  {
    phrase: 'N 個月內、N 月內',
    due: '發言日 ＋ N 個月（同一天；那個月沒有這一天就取月底）',
    example: '1 月 31 日說「一個月內」→ 2 月 28 日（閏年 29 日）',
  },
  { phrase: '半年內', due: '發言日 ＋ 6 個月', example: '8 月 27 日說「半年內」→ 隔年 2 月 27 日' },
  { phrase: 'N 年內', due: '發言日 ＋ N 年', example: '8 月 27 日說「一年內」→ 隔年 8 月 27 日' },
  { phrase: '本月底、月底前', due: '發言當月的最後一天', example: '8 月 27 日說「月底前」→ 8 月 31 日' },
  { phrase: '下個月', due: '下個月的最後一天', example: '8 月 27 日說「下個月」→ 9 月 30 日' },
  {
    phrase: '年底前、今年底、今年內',
    due: '發言那年的 12 月 31 日',
    example: '8 月 27 日說「年底前」→ 12 月 31 日',
  },
  {
    phrase: 'X 月 X 日前、X 月底前（沒有寫年）',
    due: '今年的那一天；已經過了就是明年的那一天',
    example: '8 月 27 日說「3 月底前」→ 隔年 3 月 31 日',
  },
  {
    phrase: '本會期、這個會期、會期內、會期結束前（只有立法院）',
    due: '單數會期是發言那年的 5 月 31 日，雙數會期是 12 月 31 日',
    example: '第 5 會期的 3 月 10 日說「本會期」→ 5 月 31 日',
  },
];

/** 換算不出日期的寫法（不計入追問率，也不進待追蹤清單；側寫上另外寫有幾項） */
export const DEADLINE_UNPARSED_EXAMPLES: readonly string[] = [
  '儘快',
  '盡速',
  '立即',
  '馬上',
  '下次',
  '預算審查前',
];

/* ------------------------------------------------------------------
   API 資料的整理
   ------------------------------------------------------------------ */

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function normalizeArticleRef(raw: unknown): FollowupArticle | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const slug = str(o.slug).trim();
  // 沒有 slug 就沒有頁面可連；這一項丟掉，不畫一個點了會 404 的連結
  if (!slug) return null;
  return { slug, title: str(o.title).trim(), date: str(o.date).trim() };
}

function normalizeAsk(raw: unknown): FollowupAsk | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const article = normalizeArticleRef(o.article);
  const request = str(o.request).trim();
  const dueDate = str(o.due_date).trim();
  // 清單上每一項都要有出處、內容、換得出來的到期日與看得懂的狀態；缺一樣就不列，
  // 不讓一個不完整的項目被讀者當成「待追蹤」
  if (!article || !request || !ISO_DAY.test(dueDate) || !isFollowupState(o.state)) return null;
  const followed = o.state === 'followed';
  return {
    article,
    request,
    deadline: str(o.deadline).trim(),
    due_date: dueDate,
    state: o.state,
    // 再提的報導與引用只屬於「已追問」：其他狀態帶了也不顯示
    followed_by: followed ? normalizeArticleRef(o.followed_by) : null,
    quote: followed ? str(o.quote).trim() : '',
  };
}

function normalizeJudge(raw: unknown): FollowupJudge | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const accuracy = num(o.accuracy);
  // 契約是 0～1（同議題分類器）。超出範圍不猜它是不是百分比：讀不懂就當作沒有通過
  if (accuracy === null || accuracy < 0 || accuracy > 1) return null;
  return {
    name: str(o.name),
    accuracy,
    labeled: Math.max(0, Math.round(num(o.labeled) ?? 0)),
    evaluated_at: str(o.evaluated_at),
  };
}

/**
 * 判斷器有沒有過門檻（至少 30 對人工標註、準確率至少 85%）。
 * 浮點留一點餘裕：後端算出剛好 0.85 的，傳過來不該因為捨入差一點就被擋掉。
 */
export function judgePassed(j: FollowupJudge | null | undefined): j is FollowupJudge {
  return j != null && j.labeled >= FOLLOWUP_MIN_LABELS && j.accuracy >= FOLLOWUP_MIN_ACCURACY - 1e-9;
}

/** 依到期日、再依提出的日期排；後端排過，這裡明確再排一次，不靠它剛好對 */
function byDueDate(a: FollowupAsk, b: FollowupAsk): number {
  const cmp = (x: string, y: string) => (x < y ? -1 : x > y ? 1 : 0);
  return cmp(a.due_date, b.due_date) || cmp(a.article.date, b.article.date);
}

/**
 * 追問區塊：block 是 api.ts 已經整理好 key、title、indicators 的區塊，raw 是後端原樣的物件。
 *
 * 判斷器沒過門檻（或沒給、或讀不懂）時：不給追問率、清單只留待追蹤——其他三種狀態都要靠
 * 模型判斷，沒驗證過的判斷不上頁面。跟議題分布不同，這時區塊不整個拿掉：待追蹤清單與
 * 無法換算的數量不靠模型，讀者可以自己盯（issue #24：「讀者可以自己盯」）。
 */
export function normalizeFollowupBlock(block: ProfileBlock, raw: Record<string, unknown>): ProfileBlock | null {
  if (!block.title) return null;
  const judge = normalizeJudge(raw.judge);
  const passed = judgePassed(judge);
  const asks = (Array.isArray(raw.asks) ? raw.asks : [])
    .map(normalizeAsk)
    .filter((a): a is FollowupAsk => a !== null)
    .filter((a) => passed || a.state === 'pending')
    .sort(byDueDate);
  return {
    ...block,
    indicators: passed ? block.indicators : [],
    asks,
    unparsed: Math.max(0, Math.round(num(raw.unparsed) ?? 0)),
    judge: passed ? judge : null,
  };
}

/** 清單分組：照 FOLLOWUP_STATES 的順序；判斷器沒通過時只有待追蹤一組 */
export function groupFollowups(
  asks: readonly FollowupAsk[],
  judged: boolean,
): { state: FollowupStateInfo; items: FollowupAsk[] }[] {
  return FOLLOWUP_STATES.filter((s) => judged || !s.judged).map((state) => ({
    state,
    items: asks.filter((a) => a.state === state.key),
  }));
}

/**
 * 判斷器的名字是「模型#提示詞版本#提示指紋」。指紋是給程式比對版本用的，讀者看模型與
 * 版本就好，完整字串留在 title 裡給要追究的人看（同議題分類器的寫法）。
 */
export function judgeLabel(name: string): string {
  const [modelName = '', promptVersion = ''] = (name || '').split('#');
  return promptVersion ? `${modelName}（提示詞 ${promptVersion}）` : modelName;
}

/** 報導頁的路徑。slug 來自後端，編碼過再放進 href */
export function articleHref(ref: FollowupArticle): string {
  return `/article/${encodeURIComponent(ref.slug)}`;
}
