/**
 * 多人影片（說話者由聲音區分）的資料層。
 *
 * 這個檔案只做兩件事：
 *   1. 把後端回來的東西整理成可以安全 render 的形狀。舊的 API 沒有這幾個欄位、壞掉的 API
 *      給錯型別、還有沒被代換掉的 [[Sn]] 佔位符——都不能讓頁面爆掉，也不能把原始佔位符印出來。
 *   2. 把說話者排成出場名單，配上形狀與色調（記號）。
 *
 * 「誰說了什麼」不在這裡判斷，網站也不判斷：那是 GPU 端的程式從聲音的分段查出來、後端代好
 * 名字之後才送來的，網站只顯示、從不推測。不認得的、缺的，一律往「少說」的方向退：
 * 寧可顯示「未確認身分」，也不顯示一個沒有人確認過的名字。
 *
 * 刻意不 import 其他本地模組（型別除外，執行時會被抹掉），tests/ 才能直接用 Node 載入。
 */
import type { Diarization, Point, Slide, VideoSpeaker } from './types';

/* ------------------------------------------------------------------
   固定的字樣
   ------------------------------------------------------------------ */

/** 後端把「找不到的說話者編號」換成這個；網站遇到殘留的佔位符時也換成它 */
export const UNKNOWN_DISPLAY = '（未知發言者）';
/** 後端把 S0（聲音分不出是誰）換成這個 */
export const UNDECIDED_DISPLAY = '（無法判斷）';

/** 說明列固定的一句話：說話者是機器依聲音分的，會分錯；沒有人確認名字之前只叫標籤 */
export const NEUTRAL_NOTE = '說話者由聲音自動區分，可能分錯；名稱未經確認時只以標籤稱呼。';

/** 每段截圖底下的說明：畫面只是那個時間點的截圖，不代表畫面上的人就是在說話的人 */
export const FRAME_CAPTION = '畫面僅為該時間點的截圖';

/** 警告碼 → 一句白話。不顯示任何數字或百分比 */
const WARNING_TEXT: Record<string, string> = {
  count_mismatch: '偵測到的人數與指定的不同。',
  high_unassigned: '有不少片段無法判斷是誰在說話。',
  many_minor: '出現多個短暫的聲音。',
  single_found: '只偵測到一位說話者。',
};
/** 狀態是 warn、卻沒有認得的警告碼時（或有不認得的碼時）補這一句：警告不能因為看不懂就消失 */
export const GENERIC_WARNING = '說話者的區分有不確定之處。';

/** 身分依據的文字標籤：只有編輯設過名字或角色（operator）才說「已確認」 */
export function basisLabel(basis: 'none' | 'operator'): string {
  return basis === 'operator' ? '已由編輯確認' : '未確認身分';
}

/* ------------------------------------------------------------------
   佔位符：[[Sn]] 永遠到不了頁面
   ------------------------------------------------------------------ */

/** 完整的佔位符（括號裡的空白也算） */
const PLACEHOLDER = /\[\[\s*S\d+\s*\]\]/g;
/** 被截在字串尾端的殘片：[[、[[S、[[S1、[[S1]（導言在 120 字處截斷時可能發生） */
const DANGLING = /\[\[(?:S\d*\]{0,2})?$/;

/**
 * 後端會在讀取時把 [[Sn]] 換成名字或標籤；萬一漏了（舊版後端、某個欄位沒走替換），
 * 這裡是最後一道：完整的換成「（未知發言者）」、尾端的殘片拿掉。沒有 `[[` 的字串原樣回傳。
 */
export function scrubPlaceholders(s: string): string {
  if (!s.includes('[[')) return s;
  return s.replace(PLACEHOLDER, UNKNOWN_DISPLAY).replace(DANGLING, '');
}

const MAX_DEPTH = 32;

/**
 * 整份資料的每個字串都過一遍 scrubPlaceholders。回傳新的物件，不改動傳進來的
 * （假資料是模組層級共用的）。JSON 不會比 MAX_DEPTH 深；真的更深的部分整塊丟掉，
 * 不放過任何一個沒清過的字串。
 */
export function deepScrub<T>(value: T, depth = 0): T {
  if (typeof value === 'string') return scrubPlaceholders(value) as T;
  if (Array.isArray(value)) {
    return (depth >= MAX_DEPTH ? [] : value.map((v) => deepScrub(v, depth + 1))) as T;
  }
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    if (depth >= MAX_DEPTH) return out as T;
    for (const [k, v] of Object.entries(value)) {
      if (k === '__proto__') continue;
      out[k] = deepScrub(v, depth + 1);
    }
    return out as T;
  }
  return value;
}

/* ------------------------------------------------------------------
   容錯的正規化
   ------------------------------------------------------------------ */

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const finite = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
const count = (v: unknown): number | null =>
  typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null;

/** 後端最多送 12 位；多留一倍的空間，再多就是壞資料，DOM 不能被無限撐大 */
const MAX_SPEAKERS = 24;
const MAX_POINTS = 40;
const MAX_WARNINGS = 8;

/*
 * 數字的上限，照 Pi 端的契約（services/news/articles/speaker_result.py）：超過的一定是壞資料。
 * 寧可不顯示（沒有時間連結、沒有人數），也不把亂數字印到頁面上、或做成 &t=1e%2B308s 這種連結。
 */
/** 一個時間點最大的秒數：契約的 MAX_SECONDS（一天）。影片本身預設最長 3 小時，遠遠不到 */
const CONTRACT_MAX_SECONDS = 24 * 3600;
/** 一支影片最多幾位說話者（參與者加短暫的聲音）：契約的 MAX_SPEAKERS。參與者的人數不可能比它多 */
const CONTRACT_MAX_SPEAKERS = 12;

/**
 * speakers：不是陣列、元素不是物件（舊版逐句標記時代是字串陣列）、沒有編號、編號重複的，
 * 一律丟掉。kind 只有 minor 是次要的；basis 只有剛好是 operator 才算編輯確認過。
 */
export function normalizeSpeakers(raw: unknown): VideoSpeaker[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: VideoSpeaker[] = [];
  for (const item of raw) {
    if (!isObj(item)) continue;
    const id = str(item.id).trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const label = str(item.label).trim();
    const name = str(item.name).trim();
    out.push({
      id,
      label,
      name,
      role: str(item.role).trim(),
      display: str(item.display).trim() || name || label || id,
      kind: item.kind === 'minor' ? 'minor' : 'participant',
      seconds: finite(item.seconds),
      turns: finite(item.turns),
      basis: item.basis === 'operator' ? 'operator' : 'none',
      flags: Array.isArray(item.flags) ? item.flags.filter((f): f is string => typeof f === 'string') : [],
    });
    if (out.length >= MAX_SPEAKERS) break;
  }
  return out;
}

/**
 * diarization：不是物件就是 null。只留網站要說明的四個欄位——診斷用的比例、信心不帶進來。
 * 狀態只有剛好是 ok、而且沒有警告時才算 ok；其餘一律 warn（看不懂的狀態不能被當成沒事）。
 */
export function normalizeDiarization(raw: unknown): Diarization | null {
  if (!isObj(raw)) return null;
  const warnings: string[] = [];
  if (Array.isArray(raw.warnings)) {
    for (const w of raw.warnings) {
      if (typeof w === 'string' && w && !warnings.includes(w) && warnings.length < MAX_WARNINGS) warnings.push(w);
    }
  }
  return {
    status: raw.status === 'ok' && warnings.length === 0 ? 'ok' : 'warn',
    warnings,
    requested_speakers: count(raw.requested_speakers),
    found_speakers: count(raw.found_speakers),
  };
}

/**
 * 警告要顯示的項目：每個認得的警告碼一句，不重複；有不認得的碼、或狀態是 warn 卻沒有任何
 * 認得的碼時，補一句通用的（code 是 generic）。ok 沒有項目。
 */
export function diarizationItems(d: Diarization | null): { code: string; text: string }[] {
  if (!d) return [];
  const out: { code: string; text: string }[] = [];
  let unknown = false;
  for (const code of d.warnings) {
    const text = Object.hasOwn(WARNING_TEXT, code) ? WARNING_TEXT[code] : undefined;
    if (!text) unknown = true;
    else if (!out.some((o) => o.code === code)) out.push({ code, text });
  }
  if (unknown || (d.status === 'warn' && out.length === 0)) out.push({ code: 'generic', text: GENERIC_WARNING });
  return out;
}

/** 同上，只要句子 */
export function diarizationNotes(d: Diarization | null): string[] {
  return diarizationItems(d).map((o) => o.text);
}

/**
 * points：沒有說話者編號或沒有內容的丟掉。start 不是有限的非負數、或超過一天（契約的上限）就是
 * null（重點留著，只是沒有時間與連結）。display 沒給就用編號查 speakers，查不到是「（未知發言者）」，不猜。
 */
export function normalizePoints(raw: unknown, speakers: readonly VideoSpeaker[] = []): Point[] {
  if (!Array.isArray(raw)) return [];
  const out: Point[] = [];
  for (const item of raw) {
    if (!isObj(item)) continue;
    const speaker = str(item.speaker).trim();
    const text = str(item.text).trim();
    if (!speaker || !text) continue;
    const start =
      typeof item.start === 'number' &&
      Number.isFinite(item.start) &&
      item.start >= 0 &&
      item.start <= CONTRACT_MAX_SECONDS
        ? item.start
        : null;
    const display = str(item.display).trim() || speakers.find((s) => s.id === speaker)?.display || UNKNOWN_DISPLAY;
    out.push({ speaker, display, start, text });
    if (out.length >= MAX_POINTS) break;
  }
  return out;
}

/** 每一段補上 points（舊 API 沒給就是空陣列）；不是物件的段落丟掉，其餘欄位原樣 */
export function normalizeSlides(raw: unknown, speakers: readonly VideoSpeaker[] = []): Slide[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isObj)
    .map((s) => ({ ...s, points: normalizePoints(s.points, speakers) }) as unknown as Slide);
}

/** 列表卡片的參與者人數：不是正整數、或比一支影片可能有的說話者還多（契約上限 12 位）就不顯示 */
export function cardParticipantCount(raw: unknown): number | undefined {
  return typeof raw === 'number' && Number.isInteger(raw) && raw > 0 && raw <= CONTRACT_MAX_SPEAKERS
    ? raw
    : undefined;
}

/** 參與者人數：合併後同名的算一位，短暫的聲音不算 */
export function participantCount(speakers: readonly VideoSpeaker[]): number {
  return new Set(speakers.filter((s) => s.kind === 'participant').map((s) => s.display)).size;
}

/* ------------------------------------------------------------------
   出場名單與記號
   ------------------------------------------------------------------ */

/** 形狀與色調各六種；記號是「形狀＋文字」，色調只是輔助，從不單靠顏色 */
export const SHAPES = ['circle', 'square', 'triangle', 'diamond', 'hexagon', 'pentagon'] as const;
export const TONES = 6;

export type CastMember = {
  /** 就是 display：逐字稿的行只帶顯示名，靠它對回來 */
  key: string;
  display: string;
  /** 編輯設的角色（沒有是空字串） */
  role: string;
  kind: 'participant' | 'minor';
  basis: 'none' | 'operator';
  /** 顯示名相同的（合併後的）所有說話者編號 */
  ids: string[];
  /** 0..5，色調 */
  tone: number;
  /** 0..5，形狀（對應 SHAPES） */
  shape: number;
};

export type Cast = {
  members: CastMember[];
  byId: Map<string, CastMember>;
  byDisplay: Map<string, CastMember>;
};

/** 晶片要畫的東西：member 可能是後端代入的固定字樣（無法判斷、未知），那種沒有出場資料 */
export type Mark = {
  display: string;
  kind: 'participant' | 'minor' | 'unknown';
  tone: number;
  shape: number;
};

export function markOf(member: CastMember | undefined, display: string): Mark {
  return member
    ? { display, kind: member.kind, tone: member.tone, shape: member.shape }
    : { display, kind: 'unknown', tone: 0, shape: 0 };
}

/**
 * 一個重點的說話者記號。名字以這一點自己的 display 為準；出場名單查得到、而且顯示名一致，
 * 才借用那位的形狀與色調。兩邊對不上（後端自己矛盾）時不替任何一位背書：畫成中性的
 * 「（未知發言者）」，寧可少說，也不把這句話掛在可能不對的人身上。
 * 查不到編號（或根本沒有出場名單）就是中性的記號、文字照後端給的。
 */
export function pointMark(cast: Cast | undefined, speakerId: string, display: string): Mark {
  const member = cast?.byId.get(speakerId);
  if (member && member.display !== display) return markOf(undefined, UNKNOWN_DISPLAY);
  return markOf(member, display);
}

/**
 * 出場名單：參與者在前、短暫的聲音在後，各自保持後端給的順序。
 *
 * 顯示名相同的合併成一位（後端把「合併」解開之後，兩個編號的 display 一樣）。
 * 身分依據只看「顯示名就是自己的名字或標籤」的那幾位——也就是顯示名是從他自己來的：
 * 被合併進別人的那位，顯示的是別人的字，不能拿他自己的 operator 來替那個字背書。
 * 這幾位全部是 operator 才算確認過；有一位不是，整組就當沒確認（寧可少說）。
 */
export function buildCast(speakers: readonly VideoSpeaker[]): Cast {
  const ordered = [...speakers.filter((s) => s.kind === 'participant'), ...speakers.filter((s) => s.kind === 'minor')];
  const groups = new Map<string, VideoSpeaker[]>();
  for (const s of ordered) {
    const g = groups.get(s.display);
    if (g) g.push(s);
    else groups.set(s.display, [s]);
  }

  const members: CastMember[] = [];
  const byId = new Map<string, CastMember>();
  const byDisplay = new Map<string, CastMember>();
  for (const [display, group] of groups) {
    const own = group.filter((s) => (s.name || s.label) === display);
    const owners = own.length > 0 ? own : group;
    const i = members.length;
    const member: CastMember = {
      key: display,
      display,
      role: owners.map((s) => s.role).find((r) => r) ?? '',
      kind: owners[0]?.kind ?? 'participant',
      basis: owners.every((s) => s.basis === 'operator') ? 'operator' : 'none',
      ids: group.map((s) => s.id),
      tone: (i + Math.floor(i / TONES)) % TONES,
      shape: i % SHAPES.length,
    };
    members.push(member);
    byDisplay.set(display, member);
    for (const s of group) byId.set(s.id, member);
  }
  return { members, byId, byDisplay };
}

/**
 * 逐字稿的行首可以認的顯示名：出場名單，加上後端代替佔位符的兩個固定字樣。
 * 沒有說話者的文章（議會、單人影片）回空陣列——parseTranscript 就完全不拆。
 */
export function transcriptDisplays(cast: Cast): string[] {
  if (cast.members.length === 0) return [];
  return [...cast.byDisplay.keys(), UNDECIDED_DISPLAY, UNKNOWN_DISPLAY];
}
