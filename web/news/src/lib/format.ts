const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];

/** 秒數 -> mm:ss（超過一小時會變成 h:mm:ss） */
export function mmss(seconds: number): string {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

/** 197 -> 「3 分 17 秒」 */
export function humanDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  if (m < 60) return rest ? `${m} 分 ${rest} 秒` : `${m} 分鐘`;
  const h = Math.floor(m / 60);
  return `${h} 小時 ${m % 60} 分`;
}

/** "2026-08-27" -> "2026年8月27日" */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${m[1]}年${Number(m[2])}月${Number(m[3])}日`;
}

/** "2026-08-27" -> "2026.08.27（三）" */
export function formatDateWithWeekday(iso: string | null | undefined): string {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const d = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00Z`);
  const wd = Number.isNaN(d.getTime()) ? '' : `（${WEEKDAYS[d.getUTCDay()]}）`;
  return `${m[1]}.${m[2]}.${m[3]}${wd}`;
}

export type TranscriptLine = { time: string; speaker: string; body: string };

/**
 * 逐字稿拆行：每行開頭是 mm:ss。
 *
 * 分鐘是累計的、不補到兩位以上：兩小時的影片會寫到「125:30」，所以允許三位數；
 * 只認兩位的話，一百分鐘以後的每一行都會掉出時間欄。
 *
 * 多人影片的一行是「mm:ss 顯示名：內文」。speakers 是這篇文章已知的顯示名（出場名單）：
 * 行首剛好是其中一個加上全形冒號，才拆出說話者；不是（議會的「主席：…」、影片內文裡
 * 的冒號、名字對不上的）一律整行當內文、speaker 是空字串——寧可不標，也不亂拆。
 * 沒給（或給空陣列）就完全不拆，議會與單人影片的逐字稿跟改版前逐字相同。
 */
export function parseTranscript(text: string, speakers: readonly string[] = []): TranscriptLine[] {
  if (!text) return [];
  const known = [...new Set(speakers.filter(Boolean))];
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = /^(\d{1,3}:\d{2}(?::\d{2})?)\s*(.*)$/.exec(line);
      const time = m ? m[1] : '';
      const rest = m ? m[2] : line;
      const who = known.length > 0 ? speakerOf(rest, known) : null;
      return who ? { time, speaker: who.name, body: who.body } : { time, speaker: '', body: rest };
    });
}

/**
 * 行首是不是「已知的顯示名＋全形冒號」。顯示名可以自己含全形冒號（所以不能只看第一個冒號前面），
 * 也可能是另一個顯示名的開頭（「A」與「A：B」）：吻合的不只一個時分不出是誰在說——「A：B：你好」
 * 可以是 A 說了「B：你好」，也可以是「A：B」說了「你好」——整行當內文，不猜。
 */
function speakerOf(rest: string, known: readonly string[]): { name: string; body: string } | null {
  let hit: { name: string; body: string } | null = null;
  for (const name of known) {
    if (!rest.startsWith(name)) continue;
    const tail = rest.slice(name.length).trimStart();
    if (!tail.startsWith('：')) continue;
    if (hit) return null;
    hit = { name, body: tail.slice(1).trim() };
  }
  return hit;
}

export type TranscriptGroup = { speaker: string; lines: { time: string; body: string }[] };

/**
 * 相鄰、同一位說話者的行併成一塊（晶片只畫一次）。沒有說話者的行（speaker 是空字串）
 * 不會併進有說話者的那塊；連續的無說話者行自成一塊。每一行恰好出現一次、順序不變。
 */
export function groupTranscript(lines: readonly TranscriptLine[]): TranscriptGroup[] {
  const groups: TranscriptGroup[] = [];
  for (const l of lines) {
    const last = groups[groups.length - 1];
    if (last && last.speaker === l.speaker) last.lines.push({ time: l.time, body: l.body });
    else groups.push({ speaker: l.speaker, lines: [{ time: l.time, body: l.body }] });
  }
  return groups;
}

/** 保留既有 query，換掉其中幾個值；空值就移除 */
export function buildQuery(
  base: URLSearchParams | Record<string, string | undefined>,
  patch: Record<string, string | number | undefined | null>,
): string {
  const params =
    base instanceof URLSearchParams
      ? new URLSearchParams(base)
      : new URLSearchParams(
          Object.entries(base).filter(([, v]) => v != null && v !== '') as [string, string][],
        );
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined || v === null || v === '' || v === 0) params.delete(k);
    else params.set(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

/**
 * 後端的 title 是「2026-08-27 洪毓祥－第11屆第5會期第23次會議」，
 * 卡片與文章頁另外已經顯示日期，這裡把開頭的日期拿掉避免重複。
 */
export function displayTitle(title: string): string {
  if (!title) return '';
  return title.replace(/^\s*\d{4}-\d{2}-\d{2}\s*[／/·–—-]?\s*/, '').trim() || title;
}

/**
 * "2026-10-01T06:12:00+08:00" -> "2026年10月1日 06:12"（臺灣時間）。
 *
 * 一律換成臺灣時間再顯示：後端哪天改回 UTC（結尾是 Z），直接切字串會讓
 * 「計算於」差八小時，讀者會以為資料是舊的。
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Taipei',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(d)
      .map((p) => [p.type, p.value]),
  );
  return `${parts.year}年${Number(parts.month)}月${Number(parts.day)}日 ${parts.hour}:${parts.minute}`;
}

/**
 * 指標的值：整數照寫（千分位），小數看大小決定位數。
 *
 * 「每篇 0.06 項」若一律取一位會變成 0.1，差了快一倍；大的數字（分鐘、百分比）
 * 則一位小數就夠，多了只是雜訊。
 */
export function formatMetric(value: number): string {
  const digits = Number.isInteger(value) ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return new Intl.NumberFormat('zh-TW', { maximumFractionDigits: digits }).format(value);
}

/**
 * 占比、準確率這類 0～100 的百分比：最多一位小數。
 *
 * 不用 formatMetric：它替小於 10 的值留兩位（為了「每篇 0.06 項」），但 1 ÷ 22
 * 寫成「4.55%」只是雜訊；一整欄占比也該是同一種精度，上下對齊才好比。
 */
export function formatPercent(value: number): string {
  return new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 1 }).format(value);
}
