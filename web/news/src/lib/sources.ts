/**
 * 來源（哪個議會）。後端 Article.source 的值就是這兩個代碼。
 *
 * 用語要跟著來源走：立法院的發言者是「委員」、市議會的是「議員」，原片在
 * 不同的系統。這裡集中管，頁面不要各自硬寫「立院」「IVOD」。
 */
export type ArticleSource = 'ly' | 'tccc';

export const SOURCES: ArticleSource[] = ['ly', 'tccc'];

export const SOURCE_LABEL: Record<ArticleSource, string> = {
  ly: '立法院',
  tccc: '臺中市議會',
};

export function isSource(v: string | null | undefined): v is ArticleSource {
  return v === 'ly' || v === 'tccc';
}

export function sourceLabel(source: string | undefined): string {
  return source === 'tccc' ? SOURCE_LABEL.tccc : SOURCE_LABEL.ly;
}

/** 發言者的稱謂：立委是「委員」、市議員是「議員」 */
export function memberTitle(source: string | undefined): string {
  return source === 'tccc' ? '議員' : '委員';
}

/** 原片所在的系統名稱，給「觀看原片」按鈕用 */
export function sourceSiteName(source: string | undefined): string {
  return source === 'tccc' ? '臺中市議會隨選視訊' : '立法院 IVOD';
}

/** 議員頁的英文小標 */
export function memberKicker(source: string | undefined): string {
  return source === 'tccc' ? 'City Councilor' : 'Legislator';
}
