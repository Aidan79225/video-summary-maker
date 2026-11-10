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


def diarized_result(video_id: str = CONTRACT_VIDEO, requested: int | None = None) -> dict:
    """契約範例的多人結果，改成指定那支影片的、指定人數（None＝自動）的版本。每次回傳新的副本。"""
    payload = copy.deepcopy(CONTRACT["result"])
    payload["video_id"] = video_id
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
