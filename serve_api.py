#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摘要 API 的進入點與 composition root：跑在有 GPU 與 Ollama 的那台機器上。

    uv run --group api serve_api.py

環境變數只在這個檔案裡讀。底下的模組一律靠注入拿到相依，這樣才測得動。

    SLIDEBOX_API_HOST        監聽位址，預設 127.0.0.1
    SLIDEBOX_API_PORT        監聽埠，預設 8800
    SLIDEBOX_API_KEY         設了就強制 X-API-Key；沒設則不驗（區網自用）
    SLIDEBOX_API_OUTPUT_DIR  成品落點，預設 ~/slidebox_api_output
    SLIDEBOX_MODEL           Ollama 模型（摘要、議題分類與追問判斷共用），預設同桌面 app
    SLIDEBOX_OLLAMA_HOST     Ollama 位址，預設 http://localhost:11434
    SLIDEBOX_WHISPER_DEVICE  語音辨識裝置：auto（預設）／cpu／cuda
"""
import os
import sys
import urllib.request
from functools import partial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from fastapi import FastAPI

from slidebox.composition import build_usecase
from slidebox.domain.entities import Settings
from slidebox.infrastructure.ollama_summarizer import (
    OllamaFollowUpJudge,
    OllamaTopicClassifier,
)
from slidebox_api.app import create_app
from slidebox_api.jobs import JobKind, JobStore
from slidebox_api.runner import (
    ByKindExecutor,
    FollowUpExecutor,
    JobWorker,
    SlideboxExecutor,
    TopicExecutor,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8800
OLLAMA_PROBE_TIMEOUT = 2.0
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def build_settings() -> Settings:
    settings = Settings(output_dir=os.environ.get(
        "SLIDEBOX_API_OUTPUT_DIR",
        os.path.join(os.path.expanduser("~"), "slidebox_api_output"),
    ))
    settings.model = os.environ.get("SLIDEBOX_MODEL", settings.model)
    settings.ollama_host = os.environ.get("SLIDEBOX_OLLAMA_HOST", settings.ollama_host)
    settings.detailed = True
    return settings


def ollama_probe(host: str):
    def reachable() -> bool:
        try:
            with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags",
                                        timeout=OLLAMA_PROBE_TIMEOUT):
                return True
        except Exception:  # noqa: BLE001 健康檢查失敗就是 False，不要冒泡
            return False
    return reachable


def warn_if_exposed_without_key() -> None:
    """不擋，只吵：使用者可能真的只在信任的區網裡跑。

    但「綁在對外介面又沒有任何驗證」值得在啟動時說一次，而不是等出事
    才發現——任何連得到這個埠的人都能佔用 GPU。
    """
    host = os.environ.get("SLIDEBOX_API_HOST", DEFAULT_HOST)
    if host not in LOOPBACK and not os.environ.get("SLIDEBOX_API_KEY"):
        print(f"⚠ 監聽在 {host} 但沒有設 SLIDEBOX_API_KEY：任何連得到這個埠的人"
              "都能佔用你的 GPU。", file=sys.stderr)


def build_executor(settings: Settings) -> ByKindExecutor:
    # use case 只建一次：它會連帶建立語音辨識器，模型載入要 40 秒並佔著
    # VRAM，每個工作重建一次等於每次重付。
    deck = SlideboxExecutor(build_usecase(settings), settings)
    # 分類器的 host、num_ctx 與預設模型在這裡就取值定下來：SlideboxExecutor 會
    # 把摘要工作指定的模型寫進共用的 settings，分類不能跟著它變。num_ctx 跟
    # 摘要同一個，Ollama 才不會在兩種工作之間重新載入模型。
    topic = TopicExecutor(
        partial(OllamaTopicClassifier, settings.ollama_host, num_ctx=settings.num_ctx),
        settings.model,
    )
    # 追問判斷同上：預設模型、host 與 num_ctx 在這裡定下來，不跟著摘要工作變。
    followup = FollowUpExecutor(
        partial(OllamaFollowUpJudge, settings.ollama_host, num_ctx=settings.num_ctx),
        settings.model,
    )
    return ByKindExecutor({JobKind.DECK: deck, JobKind.TOPIC: topic,
                           JobKind.FOLLOWUP: followup})


def build() -> FastAPI:
    warn_if_exposed_without_key()
    settings = build_settings()
    store = JobStore()
    executor = build_executor(settings)
    return create_app(
        store=store,
        worker=JobWorker(store, executor),
        api_key=os.environ.get("SLIDEBOX_API_KEY", ""),
        model=settings.model,
        ollama_host=settings.ollama_host,
        probe_ollama=ollama_probe(settings.ollama_host),
    )


app = build()

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("SLIDEBOX_API_HOST", DEFAULT_HOST)
    uvicorn.run(app, host=host, port=int(os.environ.get("SLIDEBOX_API_PORT",
                                                        str(DEFAULT_PORT))))
