import type { Claim, ClaimMethod, Evidence, FactCheckScore, Verdict } from './types';

/** 與後端 factchecks/scoring.py 的 MIN_SAMPLE 相同 */
export const MIN_SAMPLE = 5;

export const VERDICTS: Verdict[] = ['supported', 'partial', 'contradicted', 'unverifiable'];

export const VERDICT_LABEL: Record<Verdict, string> = {
  supported: '相符',
  partial: '部分相符',
  contradicted: '不符',
  unverifiable: '無法查證',
};

export const METHOD_LABEL: Record<ClaimMethod, string> = {
  numeric: '數字比對',
  model: '模型判讀',
  none: '未比對',
};

export const KIND_LABEL: Record<string, string> = {
  law_article: '法條內容',
  bill_content: '議案內容',
  bill_status: '議案進度',
};

const METHODS: ClaimMethod[] = ['numeric', 'model', 'none'];

export function isVerdict(v: unknown): v is Verdict {
  return typeof v === 'string' && (VERDICTS as string[]).includes(v);
}

export function emptyScore(): FactCheckScore {
  return {
    rate: null, checked: 0, supported: 0, partial: 0, contradicted: 0, unverifiable: 0,
    min_sample: MIN_SAMPLE,
  };
}

/**
 * 與後端 factchecks/scoring.py 同一條公式。只給示範資料模式用——
 * 正式站一律顯示後端算好的值，不在前端重算。
 */
export function scoreOf(verdicts: readonly string[]): FactCheckScore {
  const s = emptyScore();
  for (const v of verdicts) if (isVerdict(v)) s[v] += 1;
  s.checked = s.supported + s.partial + s.contradicted;
  s.rate = s.checked < MIN_SAMPLE ? null : (s.supported + 0.5 * s.partial + 1) / (s.checked + 2);
  return s;
}

export function formatRate(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function normalizeEvidence(raw: unknown): Evidence[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((e): e is Record<string, unknown> => !!e && typeof e === 'object')
    .map((e) => ({
      source: str(e.source),
      title: str(e.title),
      official_url: str(e.official_url),
      api_url: str(e.api_url),
      excerpt: str(e.excerpt),
    }));
}

/**
 * 後端少給或給錯的欄位不讓整頁爆掉。判定不認得的整則丟掉——
 * 顯示一個不認得的判定比不顯示更糟。
 */
export function normalizeClaims<T extends Claim = Claim>(raw: unknown): T[] {
  if (!Array.isArray(raw)) return [];
  const out: T[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const o = item as Record<string, unknown>;
    if (!isVerdict(o.verdict)) continue;
    const method = METHODS.includes(o.method as ClaimMethod) ? (o.method as ClaimMethod) : 'none';
    out.push({
      ...o,
      index: num(o.index),
      quote: str(o.quote),
      timestamp: num(o.timestamp),
      kind: str(o.kind),
      statement: str(o.statement),
      verdict: o.verdict,
      method,
      rationale: str(o.rationale),
      reviewed: o.reviewed === true,
      evidence: normalizeEvidence(o.evidence),
    } as unknown as T);
  }
  return out;
}

export function normalizeScore(raw: unknown): FactCheckScore {
  if (!raw || typeof raw !== 'object') return emptyScore();
  const o = raw as Record<string, unknown>;
  return {
    rate: typeof o.rate === 'number' && Number.isFinite(o.rate) ? o.rate : null,
    checked: num(o.checked),
    supported: num(o.supported),
    partial: num(o.partial),
    contradicted: num(o.contradicted),
    unverifiable: num(o.unverifiable),
    min_sample: num(o.min_sample) || MIN_SAMPLE,
  };
}
