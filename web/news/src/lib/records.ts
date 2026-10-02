/**
 * 院內紀錄（立委的出席、提案、記名表決）：類別與指標的對照、紀錄清單的整理、假資料。
 *
 * 數字全部由後端從立法院開放資料（LYAPI）算好，網站不重算。這裡只做三件事：
 * 1. 類別與指標的對照——證據頁的切換列、側寫的分組與方法頁都照這兩張表；
 * 2. 把 API 回的紀錄整理成可以安全 render 的形狀，缺欄位不讓整頁爆掉；
 * 3. 假資料模式：照後端的公式從假紀錄組出指標。指標與清單從同一份紀錄來，
 *    n 才保證等於清單的筆數（手寫兩份，一改就對不上）。
 *
 * 後端改了指標或類別，這裡跟方法頁要一起改。
 */
import type {
  Ballot,
  BillRecord,
  MeetingRecord,
  ProfileBlock,
  ProfileIndicator,
  RecordKind,
  RecordList,
  RecordQuery,
  Result,
  VoteRecord,
} from './types';
import fixture from '../fixtures/records.json';

/* ------------------------------------------------------------------
   類別與指標
   ------------------------------------------------------------------ */

export type RecordKindInfo = {
  key: RecordKind;
  /** 切換列上的短名 */
  label: string;
  /** 證據頁的標題 */
  title: string;
  /** 清單列的是什麼；寫在證據頁標題底下 */
  description: string;
  /** 清單的單位，跟指標的 n 同一種寫法 */
  unit: string;
  /** 清單是空的時候的說明：寫清楚是「沒有」，不是資料壞了 */
  empty: string;
  /** 這一類是哪個指標的證據：清單筆數等於它的 n */
  indicator: string;
};

/** 順序就是切換列的順序：出席、提案、表決，跟側寫的分組一樣 */
export const RECORD_KINDS: readonly RecordKindInfo[] = [
  {
    key: 'plenary',
    label: '院會',
    title: '院會出席',
    description: '他在任期間的每一場院會，以及出席名單上有沒有他。',
    unit: '場',
    empty: '這個會期在他任期內沒有院會。',
    indicator: 'plenary_attendance',
  },
  {
    key: 'committee',
    label: '委員會',
    title: '委員會出席',
    description: '他那個會期所屬委員會在他任期內的每一場會議（聯席會議只要單位裡有他的委員會就算），以及出席名單上有沒有他。',
    unit: '場',
    empty: '這個會期在他任期內，他所屬的委員會沒有開會的紀錄。',
    indicator: 'committee_attendance',
  },
  {
    key: 'proposed',
    label: '主提案',
    title: '主提案',
    description: '他列名提案人的委員提案。',
    unit: '件',
    empty: '他這個會期沒有列名提案人的委員提案。',
    indicator: 'bills_proposed',
  },
  {
    key: 'cosigned',
    label: '連署',
    title: '連署',
    description: '他列名連署人的委員提案。',
    unit: '件',
    empty: '他這個會期沒有列名連署人的委員提案。',
    indicator: 'bills_cosigned',
  },
  {
    key: 'passed',
    label: '三讀',
    title: '三讀的主提案',
    description: '他列名提案人、而且議案狀態寫著「三讀」的委員提案。',
    unit: '件',
    empty: '他這個會期主提案的議案，還沒有三讀的。',
    indicator: 'bills_passed',
  },
  {
    key: 'votes',
    label: '記名表決',
    title: '記名表決',
    description: '他在任期間的每一次記名表決：他投了什麼，以及他所屬黨團多數投的選項。',
    unit: '次表決',
    empty: '這個會期在他任期內沒有記名表決。',
    indicator: 'vote_participation',
  },
  {
    // 一致率的分母跟投票出席率不同（少了他沒投的、黨團並列的），要有自己的清單，
    // 「看這 n 次表決」點進去才剛好 n 筆
    key: 'caucus',
    label: '黨團有多數',
    title: '他有投票、黨團有多數的表決',
    description:
      '與所屬黨團一致率的分母：他有投票、而且他所屬黨團那一次有多數（票數沒有並列）的記名表決。他的票跟黨團多數相同就算一致。',
    unit: '次表決',
    empty: '這個會期沒有「他有投票、黨團也有多數」的記名表決。',
    indicator: 'caucus_agreement',
  },
  {
    key: 'defections',
    label: '跨黨投票',
    title: '跨黨投票',
    description: '他有投票、黨團也有多數，而他的票跟黨團多數不同的記名表決。',
    unit: '次表決',
    empty: '他這個會期沒有跟黨團多數不同的票。',
    indicator: 'caucus_defections',
  },
];

export function isRecordKind(v: string | null | undefined): v is RecordKind {
  // 用清單比對而不是查物件：網址參數是訪客打的，?kind=constructor 會命中原型上的屬性
  return v != null && RECORD_KINDS.some((k) => k.key === v);
}

export function recordKindInfo(kind: RecordKind): RecordKindInfo {
  return RECORD_KINDS.find((k) => k.key === kind) ?? RECORD_KINDS[0];
}

export type ChamberGroup = 'attendance' | 'bills' | 'votes';

export type ChamberIndicatorInfo = {
  key: string;
  label: string;
  unit: string;
  n_unit: string;
  /** 證據清單的類別 */
  kind: RecordKind;
  group: ChamberGroup;
  /** 比例（有分母、套最小樣本）還是計數（不套） */
  rate: boolean;
};

/** 院內紀錄的八個指標，順序就是側寫上的順序 */
export const CHAMBER_INDICATORS: readonly ChamberIndicatorInfo[] = [
  { key: 'plenary_attendance', label: '院會出席率', unit: '%', n_unit: '場', kind: 'plenary', group: 'attendance', rate: true },
  { key: 'committee_attendance', label: '委員會出席率', unit: '%', n_unit: '場', kind: 'committee', group: 'attendance', rate: true },
  { key: 'bills_proposed', label: '主提案數', unit: '件', n_unit: '件', kind: 'proposed', group: 'bills', rate: false },
  { key: 'bills_cosigned', label: '連署數', unit: '件', n_unit: '件', kind: 'cosigned', group: 'bills', rate: false },
  { key: 'bills_passed', label: '三讀數', unit: '件', n_unit: '件', kind: 'passed', group: 'bills', rate: false },
  // n_unit 跟後端（chamber.py）一樣寫「次」；連結文字另外照清單的單位寫成「次表決」
  { key: 'vote_participation', label: '投票出席率', unit: '%', n_unit: '次', kind: 'votes', group: 'votes', rate: true },
  { key: 'caucus_agreement', label: '與所屬黨團一致率', unit: '%', n_unit: '次', kind: 'caucus', group: 'votes', rate: true },
  { key: 'caucus_defections', label: '跨黨投票數', unit: '次', n_unit: '次', kind: 'defections', group: 'votes', rate: false },
];

/** 側寫上院內紀錄區塊的分組：八張卡混在一起排，出席率會跟連署數擠在同一列 */
export const CHAMBER_GROUPS: readonly { key: ChamberGroup; title: string }[] = [
  { key: 'attendance', title: '出席' },
  { key: 'bills', title: '提案' },
  { key: 'votes', title: '記名表決' },
];

export function chamberGroupOf(indicatorKey: string): ChamberGroup | null {
  return CHAMBER_INDICATORS.find((i) => i.key === indicatorKey)?.group ?? null;
}

/** 證據連結是不是院內紀錄的清單頁（不是發言者頁的報導清單） */
export function isRecordsPath(path: string | null | undefined): boolean {
  return (path ?? '').trim().startsWith('/records/');
}

/** 證據連結指到哪一類清單（網址的 kind）；不是紀錄清單頁或不認得的類別就是 null */
export function recordKindOfPath(path: string | null | undefined): RecordKind | null {
  if (!isRecordsPath(path)) return null;
  const query = (path ?? '').split('#')[0].split('?')[1] ?? '';
  const kind = new URLSearchParams(query).get('kind');
  return isRecordKind(kind) ? kind : null;
}

/**
 * 院內紀錄指標的證據連結文字：「看這 12 場」「看這 88 次表決」。
 *
 * 每一類清單的筆數都等於它對應指標的 n（一致率有自己的「黨團有多數」清單），所以可以
 * 寫出筆數。單位照連結指到的那一類清單寫：後端的 n_unit 是「次」，「看這 88 次」讀不出
 * 是表決；認不得類別才退回 n_unit。
 */
export function recordEvidenceLabel(ind: Pick<ProfileIndicator, 'n' | 'n_unit' | 'evidence_url'>): string {
  const kind = recordKindOfPath(ind.evidence_url);
  return `看這 ${ind.n} ${kind ? recordKindInfo(kind).unit : ind.n_unit}`;
}

/** 選項的中文：贊成、反對、棄權 */
export const BALLOT_LABEL: Record<Ballot, string> = { yes: '贊成', no: '反對', abstain: '棄權' };

/**
 * 指標「沒有值」的原因，寫成讀者看得懂的字；不認得的原因是空字串（頁面退回樣本不足的說法）。
 * 側寫的卡片與證據頁共用，兩邊才不會一邊寫「沒有參加黨團」、一邊寫「—」。
 */
const REASON_TEXT = new Map<string, string>([
  ['no_committee_data', '沒有他這個會期的委員會資料'],
  ['no_caucus', '沒有參加黨團'],
]);

export function reasonText(reason: string | null | undefined): string {
  // 原因字串來自後端，用 Map 查：「constructor」之類的字不能查到原型上的東西
  return (reason && REASON_TEXT.get(reason)) || '';
}

/* ------------------------------------------------------------------
   整理 API 回的紀錄
   ------------------------------------------------------------------ */

type Raw = Record<string, unknown>;

const asObject = (v: unknown): Raw | null => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Raw) : null);
// 代碼、議案編號有時是數字；只接受字串與有限的數字，其他當成沒有
const text = (v: unknown): string =>
  typeof v === 'string' ? v.trim() : typeof v === 'number' && Number.isFinite(v) ? String(v) : '';
const count = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? Math.max(0, Math.round(v)) : 0);
const notNull = <T>(v: T | null): v is T => v !== null;

// 用 Map 而不是物件：值來自上游，「constructor」之類的字不能查到原型上的東西。
// 中文也收：LYAPI 原文就是贊成／反對／棄權，後端哪天原樣轉過來不該整欄變成「沒有投票」
const BALLOTS = new Map<string, Ballot>([
  ['yes', 'yes'],
  ['no', 'no'],
  ['abstain', 'abstain'],
  ['贊成', 'yes'],
  ['反對', 'no'],
  ['棄權', 'abstain'],
]);

function ballot(v: unknown): Ballot | null {
  return typeof v === 'string' ? (BALLOTS.get(v.trim()) ?? null) : null;
}

// 後端（RecordOut）每一類都用 id 與 title：會議代碼、議案編號、表決代碼都放在 id，
// 會議名稱、議案名稱、表決議題都放在 title。舊的欄位名稱（code、name…）也收，
// 哪一邊先改都不會整份清單被當成壞掉的筆丟光
const first = (o: Raw, ...keys: string[]): string => keys.map((k) => text(o[k])).find(Boolean) ?? '';

function normalizeMeeting(raw: unknown): MeetingRecord | null {
  const o = asObject(raw);
  if (!o) return null;
  const code = first(o, 'code', 'id', 'meeting_code');
  const name = first(o, 'name', 'title');
  if (!code && !name) return null;
  return {
    code,
    date: text(o.date),
    name: name || code,
    unit: text(o.unit),
    // 只有明確的 true 才算出席：缺欄位寫成「出席」等於替他補簽到
    attended: o.attended === true,
    url: text(o.url),
  };
}

function normalizeBill(raw: unknown): BillRecord | null {
  const o = asObject(raw);
  if (!o) return null;
  const name = first(o, 'name', 'title');
  if (!name) return null;
  return {
    bill_no: first(o, 'bill_no', 'id'),
    date: text(o.date),
    name,
    status: text(o.status),
    proposers: (Array.isArray(o.proposers) ? o.proposers : []).map(text).filter(Boolean),
    url: text(o.url),
  };
}

function normalizeTally(raw: unknown): VoteRecord['tally'] {
  const o = asObject(raw);
  return o ? { yes: count(o.yes), no: count(o.no), abstain: count(o.abstain) } : null;
}

/** listCaucus：清單層級的黨團（後端只在清單上給一次，每一筆不重複帶） */
function normalizeVote(raw: unknown, listCaucus: string): VoteRecord | null {
  const o = asObject(raw);
  if (!o) return null;
  const topic = first(o, 'topic', 'title');
  const code = first(o, 'code', 'id');
  if (!topic && !code) return null;
  const caucus = text(o.caucus) || listCaucus;
  return {
    code,
    meeting_code: text(o.meeting_code),
    date: text(o.date),
    voted_at: text(o.voted_at),
    topic: topic || code,
    tally: normalizeTally(o.tally),
    vote: ballot(o.vote),
    caucus,
    // 沒有黨團就沒有黨團多數：上游哪天在這裡給了值，也不能讓一個沒參加黨團的人出現「跟黨團不同」
    caucus_majority: caucus ? ballot(o.caucus_majority) : null,
    url: text(o.url),
  };
}

/** 一列一列整理，記下丟掉幾筆 */
function normalizeRows<T>(rows: unknown[], one: (raw: unknown) => T | null): { items: T[]; dropped: number } {
  const items = rows.map(one).filter(notNull);
  return { items, dropped: rows.length - items.length };
}

/**
 * 紀錄清單：接受陣列或 {items: [...]}（後端的 RecordListOut），壞掉的筆丟掉並記下筆數。
 * 整個回應讀不懂才回 null（頁面顯示錯誤），而不是當成「沒有紀錄」——那會被讀成他一場都沒有。
 */
export function normalizeRecords(kind: RecordKind, raw: unknown): RecordList | null {
  const rows = Array.isArray(raw) ? raw : asObject(raw)?.items;
  if (!Array.isArray(rows)) return null;
  const listCaucus = text(asObject(raw)?.caucus);
  switch (kind) {
    case 'plenary':
    case 'committee':
      return { kind, ...normalizeRows(rows, normalizeMeeting) };
    case 'proposed':
    case 'cosigned':
    case 'passed':
      return { kind, ...normalizeRows(rows, normalizeBill) };
    case 'votes':
    case 'caucus':
    case 'defections':
      return { kind, ...normalizeRows(rows, (r) => normalizeVote(r, listCaucus)) };
  }
}

/* ------------------------------------------------------------------
   假資料模式（USE_FIXTURE=1）

   records.json 只存紀錄本身與編的同儕人數、百分位；三讀、黨團有多數、跨黨投票的清單
   與八個指標的值、n 都照後端的公式從紀錄算出來。紀錄清單照後端 RecordListOut 的形狀
   回（id、title、中文的票），假資料模式走的就是正式站的整理路徑。
   ------------------------------------------------------------------ */

/** 後端的 MIN_SAMPLE：比例類指標的分母不到這個數就不比較 */
const MIN_SAMPLE = 5;

type FixtureChamber = {
  person_id: number;
  session_id: number;
  /** 他那個會期的黨團；空字串＝沒有參加黨團 */
  caucus: string;
  /** 同儕人數（編的）；沒列的指標用 default */
  peers: Record<string, number>;
  /** 百分位（編的）；樣本不足時不管寫了什麼都不給 */
  percentiles: Record<string, number>;
  records: {
    plenary: MeetingRecord[];
    committee: MeetingRecord[];
    proposed: Omit<BillRecord, 'date'>[];
    cosigned: Omit<BillRecord, 'date'>[];
    votes: Omit<VoteRecord, 'caucus'>[];
  };
};

const fx = fixture as unknown as { chamber?: FixtureChamber[] };

function fixtureEntry(personId: number, sessionId: number): FixtureChamber | null {
  return (fx.chamber ?? []).find((c) => c.person_id === personId && c.session_id === sessionId) ?? null;
}

type FixtureLists = {
  plenary: MeetingRecord[];
  committee: MeetingRecord[];
  proposed: BillRecord[];
  cosigned: BillRecord[];
  passed: BillRecord[];
  votes: VoteRecord[];
  caucus: VoteRecord[];
  defections: VoteRecord[];
};

/** 一份假紀錄的八類清單；三讀、黨團有多數與跨黨投票照後端的定義篩出來 */
function fixtureLists(entry: FixtureChamber): FixtureLists {
  const bills = (rows: Omit<BillRecord, 'date'>[]): BillRecord[] => rows.map((b) => ({ date: '', ...b }));
  const votes: VoteRecord[] = entry.records.votes.map((v) => ({
    ...v,
    caucus: entry.caucus,
    caucus_majority: entry.caucus ? v.caucus_majority : null,
  }));
  // 一致率的分母：他有投票、黨團有多數（並列就沒有）
  const caucus = votes.filter((v) => v.vote && v.caucus_majority);
  const proposed = bills(entry.records.proposed);
  return {
    plenary: entry.records.plenary,
    committee: entry.records.committee,
    proposed,
    cosigned: bills(entry.records.cosigned),
    passed: proposed.filter((b) => b.status.includes('三讀')),
    votes,
    caucus,
    // 分母裡跟多數不同的
    defections: caucus.filter((v) => v.vote !== v.caucus_majority),
  };
}

const share = (part: number, whole: number) => (whole > 0 ? (part / whole) * 100 : null);

type FixtureValue = { value: number | null; n: number; reason?: string };

/** 一個指標的值與 n（公式見 spec 的指標表；方法頁寫的是同一套） */
function fixtureValue(key: string, lists: FixtureLists, caucus: string): FixtureValue {
  switch (key) {
    case 'plenary_attendance':
    case 'committee_attendance': {
      const all = key === 'plenary_attendance' ? lists.plenary : lists.committee;
      return { value: share(all.filter((m) => m.attended).length, all.length), n: all.length };
    }
    case 'vote_participation':
      return { value: share(lists.votes.filter((v) => v.vote).length, lists.votes.length), n: lists.votes.length };
    case 'caucus_agreement': {
      if (!caucus) return { value: null, n: 0, reason: 'no_caucus' };
      const agreed = lists.caucus.length - lists.defections.length;
      return { value: share(agreed, lists.caucus.length), n: lists.caucus.length };
    }
    case 'caucus_defections':
      // 後端存成 null（不是 0）：沒有參加黨團與「從不跨黨」是兩回事
      if (!caucus) return { value: null, n: 0, reason: 'no_caucus' };
      return { value: lists.defections.length, n: lists.defections.length };
    default: {
      // 三種議案都是計數：值就是件數
      const kind = CHAMBER_INDICATORS.find((i) => i.key === key)?.kind ?? 'proposed';
      return { value: lists[kind].length, n: lists[kind].length };
    }
  }
}

/**
 * 假資料的院內紀錄區塊；這個人這個會期沒有假紀錄就是 null（跟後端「沒有同步過紀錄的
 * 會期就不給這個區塊」一樣）。
 */
export function fixtureChamberBlock(personId: number, sessionId: number): ProfileBlock | null {
  const entry = fixtureEntry(personId, sessionId);
  if (!entry) return null;
  const lists = fixtureLists(entry);
  const indicators = CHAMBER_INDICATORS.map((info): ProfileIndicator => {
    const { value, n, reason } = fixtureValue(info.key, lists, entry.caucus);
    const sampleOk = !info.rate || n >= MIN_SAMPLE;
    const percentile = entry.percentiles[info.key];
    return {
      key: info.key,
      label: info.label,
      unit: info.unit,
      value,
      n,
      n_unit: info.n_unit,
      percentile: sampleOk && value !== null && typeof percentile === 'number' ? percentile : null,
      peers: entry.peers[info.key] ?? entry.peers.default ?? 0,
      sample_ok: sampleOk,
      evidence_url: `/records/${personId}?session=${sessionId}&kind=${info.kind}`,
      ...(reason ? { reason } : {}),
    };
  });
  return { key: 'chamber', title: '院內紀錄', indicators };
}

const BALLOT_ZH: Record<Ballot, string> = { yes: '贊成', no: '反對', abstain: '棄權' };
const ballotZh = (b: Ballot | null) => (b ? BALLOT_ZH[b] : null);

/** 一筆假紀錄轉成後端 RecordOut 的形狀；unit、tally、voted_at 是後端目前沒給、網站會用的欄位 */
function apiRow(kind: RecordKind, row: MeetingRecord | BillRecord | VoteRecord): Record<string, unknown> {
  if (kind === 'plenary' || kind === 'committee') {
    const m = row as MeetingRecord;
    return { id: m.code, date: m.date, title: m.name, url: m.url, meeting_code: m.code, attended: m.attended, unit: m.unit };
  }
  if (kind === 'proposed' || kind === 'cosigned' || kind === 'passed') {
    const b = row as BillRecord;
    return { id: b.bill_no, date: b.date || null, title: b.name, url: b.url, status: b.status, proposers: b.proposers };
  }
  const v = row as VoteRecord;
  return {
    id: v.code,
    date: v.date || null,
    title: v.topic,
    url: v.url,
    meeting_code: v.meeting_code,
    vote: ballotZh(v.vote),
    caucus_majority: ballotZh(v.caucus_majority),
    voted_at: v.voted_at,
    tally: v.tally,
  };
}

/** 新的在前、同一天照代碼由大到小（跟後端的排序一樣） */
function newestFirst(a: Record<string, unknown>, b: Record<string, unknown>): number {
  const key = (r: Record<string, unknown>) => [String(r.date ?? ''), String(r.id ?? '')];
  const [ad, ai] = key(a);
  const [bd, bi] = key(b);
  return bd.localeCompare(ad) || bi.localeCompare(ai);
}

/** 假資料的紀錄清單：沒有這個人這個會期的紀錄就是 404（後端同樣處理） */
export function fixtureRecords(personId: number, query: RecordQuery): Result<unknown> {
  const entry = fixtureEntry(personId, query.session);
  if (!entry) {
    return { ok: false, error: { kind: 'notfound', status: 404, message: '假資料裡沒有這個會期的院內紀錄' } };
  }
  const items = (fixtureLists(entry)[query.kind] as (MeetingRecord | BillRecord | VoteRecord)[])
    .map((row) => apiRow(query.kind, row))
    .sort(newestFirst);
  const info = recordKindInfo(query.kind);
  return {
    ok: true,
    data: { kind: query.kind, label: info.title, indicator: info.indicator, caucus: entry.caucus, count: items.length, items },
  };
}
