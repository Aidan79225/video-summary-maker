/**
 * 政黨：後端存來源給的全名（民主進步黨、中國國民黨…），縮寫與顏色只在這裡。
 * 聯合質詢的文章 party 是「甲、乙」，用 splitParties 拆。
 */
export const PARTY_SEPARATOR = '、';

const SHORT: Record<string, string> = {
  民主進步黨: '民進黨',
  中國國民黨: '國民黨',
  台灣民眾黨: '民眾黨',
  臺灣民眾黨: '民眾黨',
  時代力量: '時代力量',
  台灣基進: '台灣基進',
  無黨籍: '無黨籍',
  無: '無黨籍',
};

/** Tailwind class 組：各黨固定色，沒對到的用中性色 */
const TONE: Record<string, string> = {
  民主進步黨: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  中國國民黨: 'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  台灣民眾黨: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
  臺灣民眾黨: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
  時代力量: 'border-yellow-500/40 bg-yellow-500/10 text-yellow-700 dark:text-yellow-300',
  無黨籍: 'border-line bg-bg-soft text-muted',
  無: 'border-line bg-bg-soft text-muted',
};

export function splitParties(value: string | null | undefined): string[] {
  return (value ?? '')
    .split(PARTY_SEPARATOR)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function partyShort(name: string): string {
  return SHORT[name] ?? name;
}

export function partyTone(name: string): string {
  return TONE[name] ?? 'border-line bg-surface text-ink-2';
}
