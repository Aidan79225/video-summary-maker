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
