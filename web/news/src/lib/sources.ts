/**
 * 來源（哪個議會）。後端 Article.source 的值就是這裡的代碼。
 *
 * 用語要跟著來源走：立法院的發言者是「委員」、市議會的是「議員」，原片在
 * 不同的系統。這裡集中管，頁面不要各自硬寫「立院」「IVOD」。
 */

type SourceInfo = {
  /** 顯示名：篩選選單、來源標籤、議員頁小標 */
  label: string;
  /** 發言者的稱謂：立委是「委員」、市議員是「議員」 */
  memberTitle: string;
  /** 原片所在的系統名稱，給「觀看原片」按鈕用 */
  siteName: string;
  /** 議員頁的英文小標 */
  kicker: string;
};

/**
 * 每個來源一列，所有用語都從這張表查。
 *
 * 以前是每個函式各寫一個 `source === 'tccc' ? … : …`，只有兩個來源時還看得懂；
 * 第三個來源進來就得在每個函式裡再串一層三元運算，漏改一處就會出現「新北議員
 * 被叫成委員」這種錯。改成一張表之後，加來源只要加一列，而 `satisfies` 會在
 * 少填欄位時直接編譯失敗。
 *
 * 列的順序就是篩選選單的順序。
 */
const INFO = {
  ly: {
    label: '立法院',
    memberTitle: '委員',
    siteName: '立法院 IVOD',
    kicker: 'Legislator',
  },
  tccc: {
    label: '臺中市議會',
    memberTitle: '議員',
    siteName: '臺中市議會隨選視訊',
    kicker: 'City Councilor',
  },
} as const satisfies Record<string, SourceInfo>;

export type ArticleSource = keyof typeof INFO;

export const SOURCES = Object.keys(INFO) as ArticleSource[];

export const SOURCE_LABEL = Object.fromEntries(
  SOURCES.map((s) => [s, INFO[s].label]),
) as Record<ArticleSource, string>;

export function isSource(v: string | null | undefined): v is ArticleSource {
  // 用 SOURCES 比對而不是 `v in INFO`：網址參數是訪客打的，
  // ?source=constructor 之類的字會命中物件原型上的屬性
  return v != null && (SOURCES as string[]).includes(v);
}

/**
 * 查不到的來源一律當立法院：後端 Article.source 的預設就是 ly，
 * 早期的文章與沒帶 source 的回應都是立法院的。
 * 用語與來源標籤的顏色都經過這裡，兩邊的退路才會一致。
 */
export function toSource(source: string | null | undefined): ArticleSource {
  return isSource(source) ? source : 'ly';
}

function info(source: string | undefined): SourceInfo {
  return INFO[toSource(source)];
}

export function sourceLabel(source: string | undefined): string {
  return info(source).label;
}

/** 發言者的稱謂：立委是「委員」、市議員是「議員」 */
export function memberTitle(source: string | undefined): string {
  return info(source).memberTitle;
}

/** 原片所在的系統名稱，給「觀看原片」按鈕用 */
export function sourceSiteName(source: string | undefined): string {
  return info(source).siteName;
}

/** 議員頁的英文小標 */
export function memberKicker(source: string | undefined): string {
  return info(source).kicker;
}
