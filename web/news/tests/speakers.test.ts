/**
 * 多人影片的資料層：正規化、佔位符清除、出場名單。
 *
 * 用 Node 內建的 test runner（`npm test`），不加任何套件。被測的 src/lib/speakers.ts
 * 刻意不 import 其他本地模組（型別除外，執行時會被抹掉），Node 才載得進來。
 *
 * 每個安全閥各有一個會失敗的測試：改壞它們，對應的測試就要紅。
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  NEUTRAL_NOTE,
  UNDECIDED_DISPLAY,
  UNKNOWN_DISPLAY,
  basisLabel,
  buildCast,
  cardParticipantCount,
  deepScrub,
  diarizationItems,
  diarizationNotes,
  normalizeDiarization,
  normalizePoints,
  normalizeSlides,
  normalizeSpeakers,
  participantCount,
  pointMark,
  scrubPlaceholders,
  transcriptDisplays,
} from '../src/lib/speakers.ts';
import type { VideoSpeaker } from '../src/lib/types.ts';

/** API 的 SpeakerOut 形狀（名稱與合併已由後端解開） */
function raw(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'S1',
    label: '發言者 A',
    name: '',
    role: '',
    display: '發言者 A',
    kind: 'participant',
    seconds: 301.5,
    turns: 41,
    basis: 'none',
    flags: [],
    ...over,
  };
}

function sp(over: Partial<VideoSpeaker> = {}): VideoSpeaker {
  return {
    id: 'S1',
    label: '發言者 A',
    name: '',
    role: '',
    display: '發言者 A',
    kind: 'participant',
    seconds: 0,
    turns: 0,
    basis: 'none',
    flags: [],
    ...over,
  };
}

describe('scrubPlaceholders：原始的 [[Sn]] 不能走到頁面上', () => {
  it('完整的佔位符換成（未知發言者）', () => {
    assert.equal(scrubPlaceholders('[[S1]]：現金補助有幫助'), `${UNKNOWN_DISPLAY}：現金補助有幫助`);
    assert.equal(scrubPlaceholders('甲 [[S12]] 乙 [[S0]] 丙'), `甲 ${UNKNOWN_DISPLAY} 乙 ${UNKNOWN_DISPLAY} 丙`);
  });

  it('括號裡有空白也認得', () => {
    assert.equal(scrubPlaceholders('[[ S3 ]]：好'), `${UNKNOWN_DISPLAY}：好`);
  });

  it('被截斷在字串尾端的殘片直接拿掉', () => {
    for (const tail of ['[[', '[[S', '[[S1', '[[S1]', '[[S12]']) {
      assert.equal(scrubPlaceholders(`前面的字${tail}`), '前面的字', tail);
    }
  });

  it('沒有佔位符的字串原樣回傳，連方括號的普通文字也不動', () => {
    assert.equal(scrubPlaceholders('一般的句子'), '一般的句子');
    assert.equal(scrubPlaceholders('參見[[說明]]一節'), '參見[[說明]]一節');
    assert.equal(scrubPlaceholders('[[Smith]] 的專訪'), '[[Smith]] 的專訪');
  });

  it('清過一次的結果再清一次不會變（冪等）', () => {
    const once = scrubPlaceholders('[[S1]] 與 [[S2]] 結尾[[S');
    assert.equal(scrubPlaceholders(once), once);
  });

  it('頁面上出現的結果不含 [[S', () => {
    const out = scrubPlaceholders('a[[S1]]b[[S22]]c[[S0]]d[[S');
    assert.ok(!out.includes('[[S'), out);
  });
});

describe('deepScrub：整份文章資料的每個字串都過一遍', () => {
  it('巢狀的物件與陣列都處理，非字串原樣', () => {
    const input = {
      title: '標題 [[S1]]',
      n: 3,
      ok: true,
      nothing: null,
      slides: [{ bullets: ['[[S2]]：一', '二'], detail: '[[S1]]：甲。' }],
    };
    const out = deepScrub(input);
    assert.deepEqual(out, {
      title: `標題 ${UNKNOWN_DISPLAY}`,
      n: 3,
      ok: true,
      nothing: null,
      slides: [{ bullets: [`${UNKNOWN_DISPLAY}：一`, '二'], detail: `${UNKNOWN_DISPLAY}：甲。` }],
    });
  });

  it('不改動傳進來的物件（假資料是模組層級共用的）', () => {
    const input = { a: ['[[S1]]'], b: { c: '[[S2]]' } };
    const snapshot = JSON.stringify(input);
    deepScrub(input);
    assert.equal(JSON.stringify(input), snapshot);
  });

  it('JSON 裡的 "__proto__" 鍵不會改掉結果物件的原型，也不會被原樣帶進來', () => {
    const evil = JSON.parse('{"title":"[[S1]]","__proto__":{"polluted":"[[S2]]"},"nested":{"__proto__":{"x":1}}}');
    const out = deepScrub(evil) as Record<string, unknown>;
    assert.equal(Object.getPrototypeOf(out), Object.prototype);
    assert.equal(Object.hasOwn(out, '__proto__'), false);
    assert.equal((out as { polluted?: unknown }).polluted, undefined);
    assert.equal(out.title, UNKNOWN_DISPLAY);
    assert.equal(Object.getPrototypeOf(out.nested), Object.prototype);
    assert.equal(({} as { polluted?: unknown }).polluted, undefined, '全域的 Object.prototype 沒被污染');
  });

  it('過深的巢狀整塊丟掉，不留下任何沒清過的字串（也不會爆堆疊）', () => {
    let deep: unknown = '[[S1]]';
    for (let i = 0; i < 60; i++) deep = { child: deep };
    const out = JSON.stringify(deepScrub(deep));
    assert.ok(!out.includes('[[S'), out);
    let arr: unknown = ['[[S1]]'];
    for (let i = 0; i < 60; i++) arr = [arr];
    assert.ok(!JSON.stringify(deepScrub(arr)).includes('[[S'));
  });
});

describe('normalizeSpeakers：舊的或壞掉的 API 也不能讓頁面爆掉', () => {
  it('沒有、不是陣列、全是字串（舊版逐句標記的格式）都是空清單', () => {
    for (const bad of [undefined, null, 'x', 5, {}, ['說話者 1', '說話者 2'], [null, 3, 'S1']]) {
      assert.deepEqual(normalizeSpeakers(bad), [], JSON.stringify(bad));
    }
  });

  it('API 的形狀原樣收下', () => {
    const [s] = normalizeSpeakers([
      raw({ id: 'S2', label: '發言者 B', name: '範例乙', role: '來賓', display: '範例乙', basis: 'operator' }),
    ]);
    assert.deepEqual(s, {
      id: 'S2',
      label: '發言者 B',
      name: '範例乙',
      role: '來賓',
      display: '範例乙',
      kind: 'participant',
      seconds: 301.5,
      turns: 41,
      basis: 'operator',
      flags: [],
    });
  });

  it('display 沒給就退回名字、再退回標籤、最後才是編號', () => {
    const [a, b, c] = normalizeSpeakers([
      raw({ id: 'S1', display: undefined, name: '範例甲' }),
      raw({ id: 'S2', display: '', name: '', label: '發言者 B' }),
      raw({ id: 'S3', display: undefined, name: undefined, label: undefined }),
    ]);
    assert.equal(a?.display, '範例甲');
    assert.equal(b?.display, '發言者 B');
    assert.equal(c?.display, 'S3');
  });

  it('basis 只有剛好是 operator 才算編輯確認過；其餘一律當沒確認（寧可少說）', () => {
    const out = normalizeSpeakers([
      raw({ id: 'S1', basis: 'operator' }),
      raw({ id: 'S2', basis: 'OPERATOR' }),
      raw({ id: 'S3', basis: true }),
      raw({ id: 'S4', basis: undefined }),
      raw({ id: 'S5', basis: 'none' }),
    ]);
    assert.deepEqual(
      out.map((s) => s.basis),
      ['operator', 'none', 'none', 'none', 'none'],
    );
  });

  it('kind 只有 minor 是次要，其餘當參與者', () => {
    const out = normalizeSpeakers([raw({ id: 'S1', kind: 'minor' }), raw({ id: 'S2', kind: 'weird' })]);
    assert.deepEqual(
      out.map((s) => s.kind),
      ['minor', 'participant'],
    );
  });

  it('沒有編號的、重複的編號都丟掉（先到的算）', () => {
    const out = normalizeSpeakers([
      raw({ id: 'S1', display: '先到' }),
      raw({ id: 'S1', display: '後到' }),
      raw({ id: '' }),
      raw({ id: 7 }),
    ]);
    assert.deepEqual(
      out.map((s) => s.display),
      ['先到'],
    );
  });

  it('數字欄位不是有限數就補 0；flags 只留字串', () => {
    const [s] = normalizeSpeakers([raw({ seconds: Number.NaN, turns: 'x', flags: ['a', 1, null, 'b'] })]);
    assert.equal(s?.seconds, 0);
    assert.equal(s?.turns, 0);
    assert.deepEqual(s?.flags, ['a', 'b']);
  });

  it('人數有上限（頁面的 DOM 不能被無限撐大）', () => {
    const many = Array.from({ length: 100 }, (_, i) => raw({ id: `S${i + 1}`, display: `發言者 ${i}` }));
    assert.ok(normalizeSpeakers(many).length <= 24);
  });
});

describe('normalizeDiarization', () => {
  it('不是物件就是 null（單人影片、議會文章、舊 API）', () => {
    for (const bad of [undefined, null, 5, 'warn', [], true]) {
      assert.equal(normalizeDiarization(bad), null, JSON.stringify(bad));
    }
  });

  it('ok 的樣子', () => {
    assert.deepEqual(
      normalizeDiarization({ status: 'ok', warnings: [], requested_speakers: null, found_speakers: 2 }),
      { status: 'ok', warnings: [], requested_speakers: null, found_speakers: 2 },
    );
  });

  it('狀態只有剛好是 ok 才算 ok；看不懂的當 warn（警告不能被吞掉）', () => {
    assert.equal(normalizeDiarization({ status: 'ok' })?.status, 'ok');
    for (const s of ['warn', 'OK', '', undefined, 3, null]) {
      assert.equal(normalizeDiarization({ status: s })?.status, 'warn', String(s));
    }
  });

  it('標著 ok 卻帶著警告：照 warn，警告不能被「ok」蓋掉', () => {
    const d = normalizeDiarization({ status: 'ok', warnings: ['many_minor'], requested_speakers: 2, found_speakers: 3 });
    assert.equal(d?.status, 'warn');
    assert.deepEqual(d?.warnings, ['many_minor']);
  });

  it('人數只收非負整數：負數、小數、字串都是 null', () => {
    const n = (v: unknown) => normalizeDiarization({ status: 'ok', found_speakers: v })?.found_speakers;
    assert.equal(n(0), 0);
    assert.equal(n(3), 3);
    for (const bad of [-1, 1.5, '2', null, undefined, Number.NaN]) assert.equal(n(bad), null, String(bad));
  });

  it('warnings 只留不重複的字串；人數不是有限數就是 null', () => {
    const d = normalizeDiarization({
      status: 'warn',
      warnings: ['many_minor', 3, null, 'many_minor', 'count_mismatch'],
      requested_speakers: 'x',
      found_speakers: Number.POSITIVE_INFINITY,
    });
    assert.deepEqual(d?.warnings, ['many_minor', 'count_mismatch']);
    assert.equal(d?.requested_speakers, null);
    assert.equal(d?.found_speakers, null);
  });

  it('不留診斷用的數字（信心、比例）：回傳的物件只有四個欄位', () => {
    const d = normalizeDiarization({
      status: 'ok',
      warnings: [],
      requested_speakers: null,
      found_speakers: 2,
      unassigned_ratio: 0.012,
      overlap_ratio: 0.031,
      speech_seconds: 711.9,
      fingerprint: '0123456789abcdef',
    });
    assert.deepEqual(Object.keys(d ?? {}).sort(), ['found_speakers', 'requested_speakers', 'status', 'warnings']);
  });
});

describe('diarizationNotes：每個警告碼一句白話', () => {
  it('四個碼各自的句子', () => {
    const notes = (code: string) =>
      diarizationNotes({ status: 'warn', warnings: [code], requested_speakers: null, found_speakers: null });
    assert.deepEqual(notes('count_mismatch'), ['偵測到的人數與指定的不同。']);
    assert.deepEqual(notes('high_unassigned'), ['有不少片段無法判斷是誰在說話。']);
    assert.deepEqual(notes('many_minor'), ['出現多個短暫的聲音。']);
    assert.deepEqual(notes('single_found'), ['只偵測到一位說話者。']);
  });

  it('依序輸出多個警告', () => {
    const out = diarizationNotes({
      status: 'warn',
      warnings: ['many_minor', 'count_mismatch'],
      requested_speakers: 3,
      found_speakers: 1,
    });
    assert.deepEqual(out, ['出現多個短暫的聲音。', '偵測到的人數與指定的不同。']);
  });

  it('ok 沒有句子；沒有診斷資料也沒有', () => {
    assert.deepEqual(
      diarizationNotes({ status: 'ok', warnings: [], requested_speakers: null, found_speakers: 2 }),
      [],
    );
    assert.deepEqual(diarizationNotes(null), []);
  });

  it('warn 卻沒有認得的碼：給一句通用的話，不能讓警告消失', () => {
    const generic = ['說話者的區分有不確定之處。'];
    assert.deepEqual(
      diarizationNotes({ status: 'warn', warnings: [], requested_speakers: null, found_speakers: null }),
      generic,
    );
    assert.deepEqual(
      diarizationNotes({ status: 'warn', warnings: ['from_the_future'], requested_speakers: null, found_speakers: null }),
      generic,
    );
  });

  it('認得的碼旁邊多了不認得的碼：只多一句通用的', () => {
    const out = diarizationNotes({
      status: 'warn',
      warnings: ['many_minor', 'x', 'y'],
      requested_speakers: null,
      found_speakers: null,
    });
    assert.deepEqual(out, ['出現多個短暫的聲音。', '說話者的區分有不確定之處。']);
  });

  it('diarizationItems：每一句帶著它的警告碼（頁面的 data-warning 靠它），通用的那句是 generic', () => {
    const d = (warnings: string[]) =>
      diarizationItems({ status: 'warn', warnings, requested_speakers: null, found_speakers: null });
    assert.deepEqual(d(['single_found', 'many_minor']), [
      { code: 'single_found', text: '只偵測到一位說話者。' },
      { code: 'many_minor', text: '出現多個短暫的聲音。' },
    ]);
    assert.deepEqual(d(['nope']), [{ code: 'generic', text: '說話者的區分有不確定之處。' }]);
    assert.deepEqual(diarizationItems(null), []);
  });

  it('同一個碼重複給：只一句（不靠 normalizeDiarization 先去重）', () => {
    const out = diarizationItems({
      status: 'warn',
      warnings: ['many_minor', 'many_minor'],
      requested_speakers: null,
      found_speakers: null,
    });
    assert.equal(out.length, 1);
  });

  it('警告碼是物件原型上的名字（constructor、toString、__proto__）：當作不認得，不是句子', () => {
    const out = diarizationItems({
      status: 'warn',
      warnings: ['constructor', 'toString', '__proto__', 'hasOwnProperty'],
      requested_speakers: null,
      found_speakers: null,
    });
    assert.deepEqual(out, [{ code: 'generic', text: '說話者的區分有不確定之處。' }]);
    for (const o of out) assert.equal(typeof o.text, 'string');
  });

  it('句子裡沒有數字與百分比', () => {
    const all = diarizationNotes({
      status: 'warn',
      warnings: ['count_mismatch', 'high_unassigned', 'many_minor', 'single_found', 'zzz'],
      requested_speakers: 3,
      found_speakers: 1,
    }).join('');
    assert.ok(!/[0-9%％]/.test(all), all);
    assert.ok(!/[0-9%％]/.test(NEUTRAL_NOTE), NEUTRAL_NOTE);
  });
});

describe('normalizePoints', () => {
  const speakers = [sp({ id: 'S1', display: '範例甲' })];

  it('不是陣列就是空清單', () => {
    for (const bad of [undefined, null, {}, 'x', 3]) {
      assert.deepEqual(normalizePoints(bad, speakers), []);
    }
  });

  it('API 的形狀原樣收下', () => {
    assert.deepEqual(
      normalizePoints([{ speaker: 'S1', display: '範例甲', start: 5, text: '詢問補助成效' }], speakers),
      [{ speaker: 'S1', display: '範例甲', start: 5, text: '詢問補助成效' }],
    );
  });

  it('沒有說話者或沒有內容的重點丟掉', () => {
    const out = normalizePoints(
      [
        { speaker: '', start: 1, text: 'a' },
        { speaker: 'S1', start: 1, text: '   ' },
        { speaker: 'S1', start: 1 },
        { start: 1, text: 'b' },
        null,
        'S1：c',
        { speaker: 'S1', display: '範例甲', start: 2, text: '留下' },
      ],
      speakers,
    );
    assert.deepEqual(
      out.map((p) => p.text),
      ['留下'],
    );
  });

  it('start 不是有限的非負數就是 null（重點留著，只是不給連結）', () => {
    const out = normalizePoints(
      [
        { speaker: 'S1', start: Number.NaN, text: 'a' },
        { speaker: 'S1', start: -3, text: 'b' },
        { speaker: 'S1', start: '12', text: 'c' },
        { speaker: 'S1', start: 0, text: 'd' },
      ],
      speakers,
    );
    assert.deepEqual(
      out.map((p) => p.start),
      [null, null, null, 0],
    );
  });

  it('start 超過契約的上限（一天）就是 null：不顯示時間、不產生 &t= 連結，重點本身留著', () => {
    const out = normalizePoints(
      [
        { speaker: 'S1', start: 86400, text: 'a' },
        { speaker: 'S1', start: 86400.5, text: 'b' },
        { speaker: 'S1', start: 1e9, text: 'c' },
        { speaker: 'S1', start: 1e308, text: 'd' },
        { speaker: 'S1', start: Number.MAX_VALUE, text: 'e' },
      ],
      speakers,
    );
    assert.equal(out.length, 5);
    assert.deepEqual(
      out.map((p) => p.start),
      [86400, null, null, null, null],
    );
  });

  it('display 沒給就用編號查說話者；查不到就是（未知發言者），不猜', () => {
    const out = normalizePoints(
      [
        { speaker: 'S1', start: 1, text: 'a' },
        { speaker: 'S9', start: 2, text: 'b' },
      ],
      speakers,
    );
    assert.equal(out[0]?.display, '範例甲');
    assert.equal(out[1]?.display, UNKNOWN_DISPLAY);
  });
});

describe('normalizeSlides', () => {
  it('每段都有 points（舊 API 沒給就是空陣列），其餘欄位原樣', () => {
    const [a, b] = normalizeSlides(
      [
        { index: 1, title: '甲', bullets: ['x'], detail: 'd', timestamp: 5, image_url: null },
        {
          index: 2,
          title: '乙',
          bullets: [],
          detail: '',
          timestamp: 9,
          image_url: null,
          points: [{ speaker: 'S1', display: '範例甲', start: 9, text: '好' }],
        },
      ],
      [sp({ id: 'S1', display: '範例甲' })],
    );
    assert.deepEqual(a?.points, []);
    assert.equal(a?.title, '甲');
    assert.deepEqual(a?.bullets, ['x']);
    assert.equal(b?.points.length, 1);
  });

  it('points 的型別不對就當沒有', () => {
    const out = normalizeSlides([{ index: 1, title: 'x', points: 'oops' }, { index: 2, points: { 0: 1 } }], []);
    assert.deepEqual(
      out.map((s) => s.points),
      [[], []],
    );
  });

  it('不是陣列就是空清單；不是物件的段落丟掉', () => {
    assert.deepEqual(normalizeSlides(undefined, []), []);
    assert.deepEqual(normalizeSlides({}, []), []);
    assert.equal(normalizeSlides([null, 3, 'x', { index: 1, title: 'ok' }], []).length, 1);
  });
});

describe('buildCast：出場名單與每位說話者的記號', () => {
  it('參與者在前、次要的聲音在後，各自保持原本的順序', () => {
    const cast = buildCast([
      sp({ id: 'S3', label: '其他聲音 1', display: '其他聲音 1', kind: 'minor' }),
      sp({ id: 'S1', display: '發言者 A' }),
      sp({ id: 'S2', label: '發言者 B', display: '發言者 B' }),
    ]);
    assert.deepEqual(
      cast.members.map((m) => m.display),
      ['發言者 A', '發言者 B', '其他聲音 1'],
    );
    assert.deepEqual(
      cast.members.map((m) => m.kind),
      ['participant', 'participant', 'minor'],
    );
  });

  it('沒有說話者就是空名單', () => {
    const cast = buildCast([]);
    assert.equal(cast.members.length, 0);
    assert.equal(cast.byId.size, 0);
  });

  it('同一個顯示名（合併後）只出現一次，兩個編號都查得到它', () => {
    const cast = buildCast([
      sp({ id: 'S1', display: '發言者 A' }),
      sp({ id: 'S3', label: '發言者 C', display: '發言者 A' }),
    ]);
    assert.equal(cast.members.length, 1);
    assert.equal(cast.byId.get('S1'), cast.byId.get('S3'));
    assert.deepEqual(cast.members[0]?.ids, ['S1', 'S3']);
    assert.equal(cast.byDisplay.get('發言者 A'), cast.members[0]);
  });

  it('前六位的形狀與色調互不相同，十二位內的組合也不重複', () => {
    const twelve = Array.from({ length: 12 }, (_, i) =>
      sp({ id: `S${i + 1}`, label: `發言者 ${i}`, display: `發言者 ${i}` }),
    );
    const cast = buildCast(twelve);
    const combos = new Set(cast.members.map((m) => `${m.tone}/${m.shape}`));
    assert.equal(combos.size, 12);
    assert.equal(new Set(cast.members.slice(0, 6).map((m) => m.shape)).size, 6);
    assert.equal(new Set(cast.members.slice(0, 6).map((m) => m.tone)).size, 6);
  });

  describe('身分依據（basis）：寧可少說，不可多說', () => {
    it('編輯設了名字的是「operator」，沒設的是「none」', () => {
      const cast = buildCast([
        sp({ id: 'S1', name: '範例甲', display: '範例甲', basis: 'operator', role: '主持人' }),
        sp({ id: 'S2', label: '發言者 B', display: '發言者 B' }),
      ]);
      assert.equal(cast.byId.get('S1')?.basis, 'operator');
      assert.equal(cast.byId.get('S1')?.role, '主持人');
      assert.equal(cast.byId.get('S2')?.basis, 'none');
      assert.equal(cast.byId.get('S2')?.role, '');
    });

    it('合併進已確認名字的那位：顯示的是編輯給的名字，整組算確認過', () => {
      const cast = buildCast([
        sp({ id: 'S1', name: '範例甲', display: '範例甲', basis: 'operator' }),
        sp({ id: 'S3', label: '發言者 C', display: '範例甲', basis: 'none' }),
      ]);
      assert.equal(cast.members.length, 1);
      assert.equal(cast.members[0]?.basis, 'operator');
    });

    it('編輯給了名字、卻被合併進一位沒確認的：顯示的是別人的標籤，不能說成已確認', () => {
      const cast = buildCast([
        sp({ id: 'S1', label: '發言者 A', display: '發言者 A', basis: 'none' }),
        sp({ id: 'S3', label: '發言者 C', name: '範例丙', display: '發言者 A', basis: 'operator' }),
      ]);
      assert.equal(cast.members.length, 1);
      assert.equal(cast.members[0]?.basis, 'none');
    });

    it('兩個本人撞了同一個顯示名，只要有一個沒確認，整組就當沒確認', () => {
      const cast = buildCast([
        sp({ id: 'S1', label: '發言者 A', display: '發言者 A', basis: 'none' }),
        sp({ id: 'S2', label: '發言者 B', name: '發言者 A', display: '發言者 A', basis: 'operator' }),
      ]);
      assert.equal(cast.members[0]?.basis, 'none');
    });

    it('basisLabel：只有 operator 說「已由編輯確認」', () => {
      assert.equal(basisLabel('operator'), '已由編輯確認');
      assert.equal(basisLabel('none'), '未確認身分');
    });
  });
});

describe('pointMark：一個重點的說話者記號', () => {
  const cast = buildCast([sp({ id: 'S1', display: '範例甲' }), sp({ id: 'S2', display: '其他聲音 1', kind: 'minor' })]);

  it('編號查得到、顯示名一致：借用那位的種類、形狀與色調，文字照後端給的', () => {
    const a = pointMark(cast, 'S1', '範例甲');
    assert.deepEqual(a, { display: '範例甲', kind: 'participant', tone: cast.byId.get('S1')?.tone, shape: cast.byId.get('S1')?.shape });
    const b = pointMark(cast, 'S2', '其他聲音 1');
    assert.equal(b.kind, 'minor');
    assert.equal(b.shape, cast.byId.get('S2')?.shape);
    assert.notEqual(b.shape, a.shape);
  });

  it('編號查不到（或根本沒有出場名單）：中性的記號，文字照後端給的，不猜', () => {
    assert.deepEqual(pointMark(cast, 'S9', UNKNOWN_DISPLAY), { display: UNKNOWN_DISPLAY, kind: 'unknown', tone: 0, shape: 0 });
    assert.deepEqual(pointMark(undefined, 'S1', '範例甲'), { display: '範例甲', kind: 'unknown', tone: 0, shape: 0 });
  });

  it('編號查得到、但顯示名跟出場名單對不上（後端自己矛盾）：不替任何一位背書，畫成（未知發言者）', () => {
    // S2 是短暫的聲音（形狀、色調都不是預設）：若借了它的記號，這裡會是 minor
    assert.deepEqual(pointMark(cast, 'S2', '範例甲'), { display: UNKNOWN_DISPLAY, kind: 'unknown', tone: 0, shape: 0 });
    assert.deepEqual(pointMark(cast, 'S1', '其他聲音 1'), { display: UNKNOWN_DISPLAY, kind: 'unknown', tone: 0, shape: 0 });
  });
});

describe('其他小工具', () => {
  it('participantCount：只數參與者，合併後同名的算一位', () => {
    assert.equal(
      participantCount([
        sp({ id: 'S1' }),
        sp({ id: 'S2', display: '發言者 B' }),
        sp({ id: 'S3', display: '發言者 B' }),
        sp({ id: 'S4', display: '其他聲音 1', kind: 'minor' }),
      ]),
      2,
    );
    assert.equal(participantCount([]), 0);
  });

  it('transcriptDisplays：說話者的顯示名，加上後端代替佔位符的兩個固定字樣', () => {
    const cast = buildCast([sp({ id: 'S1', display: '範例甲' }), sp({ id: 'S2', display: '發言者 B' })]);
    const known = transcriptDisplays(cast);
    assert.ok(known.includes('範例甲'));
    assert.ok(known.includes('發言者 B'));
    assert.ok(known.includes(UNDECIDED_DISPLAY));
    assert.ok(known.includes(UNKNOWN_DISPLAY));
  });

  it('沒有說話者的文章不認任何前綴（議會的逐字稿完全不拆）', () => {
    assert.deepEqual(transcriptDisplays(buildCast([])), []);
  });

  it('cardParticipantCount：列表卡片的人數只收正整數，其餘不顯示', () => {
    assert.equal(cardParticipantCount(2), 2);
    assert.equal(cardParticipantCount(12), 12);
    // 契約：一支影片最多 12 位說話者（參與者加短暫的聲音），人數不可能比它多
    for (const huge of [13, 25, 1e21, Number.MAX_SAFE_INTEGER]) {
      assert.equal(cardParticipantCount(huge), undefined, String(huge));
    }
    for (const bad of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, '2', null, undefined, {}, [2], true]) {
      assert.equal(cardParticipantCount(bad), undefined, String(bad));
    }
  });
});
