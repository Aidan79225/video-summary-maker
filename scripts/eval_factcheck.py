#!/usr/bin/env python3
"""事實查核的準確率評估：拿人工標註的樣本跑整條 pipeline，印出混淆矩陣。

    uv run python scripts/eval_factcheck.py scripts/factcheck_labels.jsonl

需要 Ollama 與網路（會真的查 LYAPI）。重點看兩個數字：
- 「相符」的精確率：這一類自動發佈又計分，判錯會灌高分數
- 「不符」的召回率：漏掉的會被當成相符或無法查證
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from factcheck.composition import build_factcheck  # noqa: E402
from factcheck.domain.entities import Speech, Verdict  # noqa: E402

MISSED = "（沒有抽出）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels")
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--num-ctx", type=int, default=32768)
    args = parser.parse_args()

    usecase = build_factcheck(args.ollama, args.model, args.num_ctx)
    confusion: Counter[tuple[str, str]] = Counter()
    with open(args.labels, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]

    for item in items:
        speech = Speech(item["speaker"], date.fromisoformat(item["date"]),
                        item.get("meeting", ""), item["transcript"])
        checked = usecase.execute(speech, lambda fraction, status: None, lambda: False)
        print(f"\n== {item.get('ivod_id', '')} {item['speaker']} {item['date']}：抽出 {len(checked)} 則")
        for c in checked:
            print(f"   [{c.verdict} / {c.method}] 「{c.claim.quote[:40]}」→ {c.rationale[:90]}")
        for expected in item["expected"]:
            hit = next((c for c in checked if expected["quote_contains"] in c.claim.quote), None)
            predicted = str(hit.verdict) if hit else MISSED
            mark = "✓" if predicted == expected["verdict"] else "✗"
            print(f"   {mark} 標註「{expected['quote_contains']}」應為 {expected['verdict']}，得到 {predicted}")
            confusion[(expected["verdict"], predicted)] += 1

    columns = [v.value for v in Verdict] + [MISSED]
    print("\n混淆矩陣（列＝標註，欄＝得到）")
    print("".ljust(14) + "".join(c.ljust(14) for c in columns))
    for row in [v.value for v in Verdict]:
        print(row.ljust(14) + "".join(str(confusion[(row, c)]).ljust(14) for c in columns))

    total = sum(confusion.values())
    correct = sum(n for (e, p), n in confusion.items() if e == p)
    predicted_supported = sum(n for (e, p), n in confusion.items() if p == Verdict.SUPPORTED)
    true_supported = confusion[(Verdict.SUPPORTED.value, Verdict.SUPPORTED.value)]
    actual_contradicted = sum(n for (e, p), n in confusion.items() if e == Verdict.CONTRADICTED)
    caught = confusion[(Verdict.CONTRADICTED.value, Verdict.CONTRADICTED.value)]
    print(f"\n標註 {total} 則，正確 {correct} 則")
    if predicted_supported:
        print(f"「相符」精確率：{true_supported}/{predicted_supported}")
    if actual_contradicted:
        print(f"「不符」召回率：{caught}/{actual_contradicted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
