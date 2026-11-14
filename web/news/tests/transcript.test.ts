/**
 * 逐字稿拆行與依說話者分組。
 *
 * 議會與單人影片的逐字稿一行都不能變：沒有已知說話者時，parseTranscript 的結果
 * 要跟改版前逐字相同（body 保留整行「主席：請…」，不拆冒號）。
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { parseTranscript, groupTranscript } from '../src/lib/format.ts';

describe('parseTranscript：沒有說話者清單時與改版前完全相同', () => {
  it('議會的逐字稿：冒號留在內文裡，不拆', () => {
    assert.deepEqual(parseTranscript('00:06 主席：請範例四委員質詢。\n00:11 範例四委員：謝謝主席，請次長。'), [
      { time: '00:06', speaker: '', body: '主席：請範例四委員質詢。' },
      { time: '00:11', speaker: '', body: '範例四委員：謝謝主席，請次長。' },
    ]);
  });

  it('三位數的分鐘、h:mm:ss、沒有時間的行', () => {
    assert.deepEqual(parseTranscript('100:05 順序主要看兩件事\n1:02:03 超過一小時\n沒有時間的一行'), [
      { time: '100:05', speaker: '', body: '順序主要看兩件事' },
      { time: '1:02:03', speaker: '', body: '超過一小時' },
      { time: '', speaker: '', body: '沒有時間的一行' },
    ]);
  });

  it('空字串、空白行、CRLF', () => {
    assert.deepEqual(parseTranscript(''), []);
    assert.deepEqual(parseTranscript('00:01 甲\r\n\r\n  \r\n00:02 乙\r\n'), [
      { time: '00:01', speaker: '', body: '甲' },
      { time: '00:02', speaker: '', body: '乙' },
    ]);
  });

  it('給空的說話者清單也一樣不拆', () => {
    assert.deepEqual(parseTranscript('00:06 主席：請質詢。', []), [{ time: '00:06', speaker: '', body: '主席：請質詢。' }]);
  });
});

describe('parseTranscript：有說話者清單時', () => {
  const known = ['範例甲', '發言者 B', '其他聲音 1'];

  it('第一個全形冒號前面剛好是已知的顯示名，才拆出說話者', () => {
    assert.deepEqual(parseTranscript('01:16 發言者 B：至少要把名額加倍。', known), [
      { time: '01:16', speaker: '發言者 B', body: '至少要把名額加倍。' },
    ]);
  });

  it('分鐘超過 99 照樣拆', () => {
    assert.deepEqual(parseTranscript('125:30 範例甲：先把名額補足。', known), [
      { time: '125:30', speaker: '範例甲', body: '先把名額補足。' },
    ]);
  });

  it('內文裡還有冒號：只用第一個，後面的留在內文', () => {
    assert.deepEqual(parseTranscript('00:41 範例甲：重點有三：申請資格、抽籤方式、候補順序。', known), [
      { time: '00:41', speaker: '範例甲', body: '重點有三：申請資格、抽籤方式、候補順序。' },
    ]);
  });

  it('前綴不是已知的說話者：整行當內文，不亂拆（議會式的「主席：」也一樣）', () => {
    assert.deepEqual(parseTranscript('27:45 現場字幕：這一行的開頭不是任何說話者。', known), [
      { time: '27:45', speaker: '', body: '現場字幕：這一行的開頭不是任何說話者。' },
    ]);
    assert.deepEqual(parseTranscript('00:06 主席：請質詢。', known), [{ time: '00:06', speaker: '', body: '主席：請質詢。' }]);
  });

  it('內文中間出現某位說話者的名字加冒號，不影響拆法', () => {
    assert.deepEqual(parseTranscript('00:10 發言者 B：他引用了「範例甲：你好」這句。', known), [
      { time: '00:10', speaker: '發言者 B', body: '他引用了「範例甲：你好」這句。' },
    ]);
  });

  it('只認全形冒號；半形的不算', () => {
    assert.deepEqual(parseTranscript('00:10 發言者 B: 半形冒號', known), [
      { time: '00:10', speaker: '', body: '發言者 B: 半形冒號' },
    ]);
  });

  it('名字只是另一個名字的開頭：必須整個吻合', () => {
    const names = ['王', '王小明'];
    assert.deepEqual(parseTranscript('00:01 王小明：你好\n00:02 王：嗨\n00:03 王小：誰', names), [
      { time: '00:01', speaker: '王小明', body: '你好' },
      { time: '00:02', speaker: '王', body: '嗨' },
      { time: '00:03', speaker: '', body: '王小：誰' },
    ]);
  });

  it('沒有時間的行也能拆；冒號後面是空的也行', () => {
    assert.deepEqual(parseTranscript('發言者 B：沒有時間\n00:05 範例甲：', known), [
      { time: '', speaker: '發言者 B', body: '沒有時間' },
      { time: '00:05', speaker: '範例甲', body: '' },
    ]);
  });

  it('清單裡的空字串不當成說話者（否則每一行開頭的「：」都被拆）', () => {
    assert.deepEqual(parseTranscript('00:05 ：怪行', ['', '範例甲']), [{ time: '00:05', speaker: '', body: '：怪行' }]);
  });

  it('顯示名與冒號之間多一個空白也認得', () => {
    assert.deepEqual(parseTranscript('00:01 範例甲 ：你好', known), [{ time: '00:01', speaker: '範例甲', body: '你好' }]);
  });

  it('冒號後面的空白不算內文', () => {
    assert.deepEqual(parseTranscript('00:01 範例甲： 你好', known), [{ time: '00:01', speaker: '範例甲', body: '你好' }]);
  });

  it('顯示名本身含全形冒號：要整個名字吻合才算（只看第一個冒號會認不出來）', () => {
    assert.deepEqual(parseTranscript('00:01 範例：甲：你好', ['範例：甲', '乙']), [
      { time: '00:01', speaker: '範例：甲', body: '你好' },
    ]);
  });

  it('兩個已知的顯示名都吻合行首（「A」與「A：B」）：分不出是誰，整行當內文，不猜；只吻合一個的照常拆', () => {
    const names = ['A', 'A：B'];
    assert.deepEqual(parseTranscript('00:01 A：B：你好\n00:02 A：嗨\n00:03 A：C：再見\n00:04 A：B：', names), [
      { time: '00:01', speaker: '', body: 'A：B：你好' },
      { time: '00:02', speaker: 'A', body: '嗨' },
      { time: '00:03', speaker: 'A', body: 'C：再見' },
      { time: '00:04', speaker: '', body: 'A：B：' },
    ]);
  });

  it('清單的順序不影響結果（長的在前或在後都一樣）', () => {
    const line = '00:01 A：B：你好';
    assert.deepEqual(parseTranscript(line, ['A', 'A：B']), parseTranscript(line, ['A：B', 'A']));
  });
});

describe('groupTranscript：相鄰且同一位說話者的行併成一塊', () => {
  it('同一位連續的行成一塊，中間隔了別人就分開', () => {
    const lines = parseTranscript(
      ['00:05 範例甲：一', '00:14 發言者 B：二', '00:26 發言者 B：三', '00:31 範例甲：四', '01:02 範例甲：五'].join('\n'),
      ['範例甲', '發言者 B'],
    );
    const groups = groupTranscript(lines);
    assert.deepEqual(
      groups.map((g) => [g.speaker, g.lines.map((l) => l.body)]),
      [
        ['範例甲', ['一']],
        ['發言者 B', ['二', '三']],
        ['範例甲', ['四', '五']],
      ],
    );
  });

  it('每一行恰好出現一次、順序不變', () => {
    const lines = parseTranscript('00:01 甲：a\n00:02 甲：b\n00:03 乙：c\n沒有人說的一行\n00:05 甲：d', ['甲', '乙']);
    const flat = groupTranscript(lines).flatMap((g) => g.lines);
    assert.deepEqual(flat, lines.map(({ time, body }) => ({ time, body })));
  });

  it('沒有說話者的行不會被併進有說話者的那塊；連續的無人行自成一塊', () => {
    const lines = parseTranscript('00:01 甲：a\n現場字幕：x\n另一行\n00:04 甲：b', ['甲']);
    assert.deepEqual(
      groupTranscript(lines).map((g) => [g.speaker, g.lines.length]),
      [
        ['甲', 1],
        ['', 2],
        ['甲', 1],
      ],
    );
  });

  it('空的輸入沒有任何一塊', () => {
    assert.deepEqual(groupTranscript([]), []);
  });
});
