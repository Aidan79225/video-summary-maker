/**
 * 來源。後端 Article.source 的值就是這裡的代碼。
 *
 * 來源分兩種（kind）：council 是議會（立法院、臺中市議會、新北市議會），有首頁、
 * 篩選器與人物側寫；video 是手動加入的影片（YouTube），不是議會，只出現在 /videos。
 *
 * 用語要跟著來源走：立法院的發言者是「委員」、市議會的是「議員」、影片的是「頻道」，
 * 原片在不同的系統。這裡集中管，頁面不要各自硬寫「立院」「IVOD」。
 */
import { displayTitle } from './format';

type SourceInfo = {
  /** 顯示名：篩選選單、來源標籤、議員頁小標 */
  label: string;
  /** 發言者的稱謂：立委是「委員」、市議員是「議員」、YouTube 是「頻道」 */
  memberTitle: string;
  /** 原片所在的系統名稱，給「觀看原片」按鈕用 */
  siteName: string;
  /** 議員頁的英文小標 */
  kicker: string;
  /**
   * council 是議會：首頁、報頭小字與來源篩選只認這一類。
   * video 是手動加入的影片：不當成議會，只出現在 /videos，後端沒帶 source 時也不回它。
   */
  kind: 'council' | 'video';
  /** 原片網址吃不吃時間參數；吃的話，逐段的時間戳會帶 t= 直接跳到那一段 */
  timeParam: boolean;
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
    kind: 'council',
    timeParam: false,
  },
  tccc: {
    label: '臺中市議會',
    memberTitle: '議員',
    siteName: '臺中市議會隨選視訊',
    kicker: 'City Councilor',
    kind: 'council',
    timeParam: false,
  },
  ntpc: {
    label: '新北市議會',
    memberTitle: '議員',
    siteName: '新北市議會議事影音',
    kicker: 'City Councilor',
    kind: 'council',
    timeParam: false,
  },
  youtube: {
    label: 'YouTube',
    memberTitle: '頻道',
    siteName: 'YouTube',
    kicker: 'Channel',
    kind: 'video',
    timeParam: true,
  },
} as const satisfies Record<string, SourceInfo>;

export type ArticleSource = keyof typeof INFO;

/** 所有來源，含影片。要列「議會」的地方用 COUNCIL_SOURCES，不要用這個 */
export const SOURCES = Object.keys(INFO) as ArticleSource[];

/**
 * 議會來源：報頭小字、頁首與首頁的來源篩選只列這幾個，後端沒帶 source 時的
 * 預設範圍也是這一組。
 *
 * 不能直接用 SOURCES：影片也在表裡，直接用的話報頭會寫成「立法院 · 臺中市議會 ·
 * 新北市議會 · YouTube」把影片說成議會，首頁的來源篩選也會多一個點進去全是影片的選項。
 */
export const COUNCIL_SOURCES = SOURCES.filter((s) => INFO[s].kind === 'council');

/** 每個來源（含影片）的顯示名：發言者選單與文章卡片的來源標籤都要查得到影片 */
export const SOURCE_LABEL = Object.fromEntries(
  SOURCES.map((s) => [s, INFO[s].label]),
) as Record<ArticleSource, string>;

export function isSource(v: string | null | undefined): v is ArticleSource {
  // 用 SOURCES 比對而不是 `v in INFO`：網址參數是訪客打的，
  // ?source=constructor 之類的字會命中物件原型上的屬性
  return v != null && (SOURCES as string[]).includes(v);
}

/**
 * 首頁的 ?source= 只認議會：影片有自己的頁面（/videos），不在首頁的篩選裡。
 * 手打 ?source=youtube 的當成沒篩，跟其他亂打的來源一樣。
 */
export function isCouncilSource(v: string | null | undefined): v is ArticleSource {
  return isSource(v) && INFO[v].kind === 'council';
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

/** 發言者的稱謂：立委是「委員」、市議員是「議員」、YouTube 是「頻道」 */
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

/** 是不是手動加入的影片（不是議會）：會議、長度、日期連結、頁首返回鍵都要跟著分開 */
export function isVideoSource(source: string | undefined): boolean {
  return info(source).kind === 'video';
}

/**
 * 卡片與文章頁顯示的標題。
 *
 * 議會的標題是「2026-08-27 洪毓祥－第11屆第5會期第23次會議」，日期頁面另外有，
 * displayTitle 把開頭的日期拿掉。影片的標題是影片自己的，開頭的日期是標題的一部分
 * （「2026-10-19 直播」），拿掉只會剩半句、甚至留下孤零零的分隔符，所以原樣顯示。
 */
export function articleTitle(source: string | undefined, title: string): string {
  return isVideoSource(source) ? title : displayTitle(title);
}

/**
 * 逐段時間戳的連結。
 *
 * 原片網址吃時間參數的來源（YouTube）接上 t=<秒>s，點了直接從那一段開始播；
 * 其餘維持原網址（IVOD 之類只能從頭播）。以來源判斷而不是看網址的主機名稱，
 * 假資料用的假網址也一樣動；用 URL 而不是字串相接，網址原本已經帶 t= 時會被換掉、
 * 不會變成兩個。第 0 秒不加：從頭播本來就是 0。
 */
export function timestampHref(
  source: string | undefined,
  url: string | null,
  seconds: number,
): string | null {
  if (!url || !info(source).timeParam) return url;
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s === 0) return url;
  try {
    const u = new URL(url);
    u.searchParams.set('t', `${s}s`);
    return u.toString();
  } catch {
    return url;
  }
}

/** 時間戳連結的說明（title）：吃時間參數的來源說會從這一段開始，其餘照實說會從頭播 */
export function timestampHint(source: string | undefined, time: string): string {
  const i = info(source);
  return i.timeParam
    ? `在${i.siteName}開啟原始影片，從 ${time} 開始播放`
    : `在${i.siteName}開啟原始影片（原片網址不支援時間參數，會從頭播放）`;
}
