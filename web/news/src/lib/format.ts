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

/** 逐字稿拆行：每行開頭是 mm:ss */
export function parseTranscript(text: string): { time: string; body: string }[] {
  if (!text) return [];
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = /^(\d{1,2}:\d{2}(?::\d{2})?)\s*(.*)$/.exec(line);
      return m ? { time: m[1], body: m[2] } : { time: '', body: line };
    });
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
