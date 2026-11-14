"""多人影片測試共用的東西：契約檔（GPU 與 Pi 共用的範例）、能力回應、換成某支影片的結果。

契約檔 fixtures/speaker_contract_v1.json 與 GPU 那邊逐位元組相同（test_speaker_contract.py 會比對）；
測試拿它當「GPU 真的會回的樣子」，不另外手寫一份會慢慢跟契約脫節的結果。
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import date
from pathlib import Path

from articles.youtube_source import canonical_url

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "speaker_contract_v1.json").read_text("utf-8"))
# 契約範例的影片 id（結果裡的 video_id）
CONTRACT_VIDEO = CONTRACT["result"]["video_id"]
# 契約 v1 的 /capabilities 回應：GPU 已經更新、可以區分說話者
READY_CAPABILITIES = CONTRACT["capabilities"]


def run_with_deadline(function, *args, seconds: float = 5.0):
    """在另一條執行緒裡呼叫 function(*args)；超過 seconds 秒還沒回來，測試就失敗（而不是整個測試跑到永遠）。

    給「壞資料不能讓程式無限迴圈」這類測試用：守衛被拿掉的時候，要看到明確的失敗，不是一支卡住的測試。
    function 丟出的例外原樣交回給測試。卡住的執行緒是 daemon，測試程序結束時一起結束。
    """
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = function(*args)
        except BaseException as error:      # noqa: BLE001 - 原樣交回呼叫的測試
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AssertionError(f"{getattr(function, '__name__', 'function')} 在 {seconds:g} 秒內沒有回來（無限迴圈？）")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def rendered(points: list[dict]) -> tuple[list[str], str]:
    """GPU 的 render_slide：條列＝前 4 個重點各一項「[[Sn]]：重點」，完整敘述＝連續同一位說話者的重點併成一段
    「[[Sn]]：甲；乙。」、段與段之間一個空格。測試改了重點之後用它重排，條列和完整敘述才會跟著對得上。"""
    bullets = [f"[[{p['speaker']}]]：{p['text']}" for p in points[:4]]
    runs: list[tuple[str, list[str]]] = []
    for point in points:
        if runs and runs[-1][0] == point["speaker"]:
            runs[-1][1].append(point["text"])
        else:
            runs.append((point["speaker"], [point["text"]]))
    return bullets, " ".join(f"[[{speaker}]]：{'；'.join(texts)}。" for speaker, texts in runs)


def rebuild(slide: dict) -> dict:
    """重點改了之後，這一頁的條列與完整敘述照 GPU 的規則重排（原地改，回傳同一頁）。"""
    slide["bullets"], slide["detail"] = rendered(slide["points"])
    return slide


def video_keys(video_id: str) -> dict:
    """結果裡說「我是哪一支影片」的兩個鍵：video_id 與 source_url（GPU 交出的是送給它的那個正規網址）。"""
    return {"video_id": video_id, "source_url": canonical_url(video_id)}


def diarized_result(video_id: str = CONTRACT_VIDEO, requested: int | None = None) -> dict:
    """契約範例的多人結果，改成指定那支影片的、指定人數（None＝自動）的版本。每次回傳新的副本。"""
    payload = copy.deepcopy(CONTRACT["result"])
    payload.update(video_keys(video_id))
    payload["diarization"]["requested_speakers"] = requested
    return payload


def publish_diarized(video_id: str = "jNQXAC9IVRw", speaker_count: int | None = None,
                     day: date = date(2026, 10, 30), title: str = "少子化對談"):
    """已發佈的多人影片：用契約範例的結果走一遍 register_video → save_result，存進去的就是真正會有的樣子。

    要放在測試的 setUp 裡呼叫（要有資料庫、媒體資料夾要已經換成暫存的）。
    """
    from articles.ingest import register_video, save_result
    from articles.models import Article

    article, _ = register_video(video_id, title=title, speaker="示範頻道", day=day, duration_seconds=900,
                                speaker_count=speaker_count)
    save_result(article, diarized_result(video_id, requested=speaker_count if (speaker_count or 0) >= 2 else None))
    return Article.objects.get(pk=article.pk)
