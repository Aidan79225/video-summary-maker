#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摘要 API 的進入點與 composition root：跑在有 GPU 與 Ollama 的那台機器上。

    uv run --group api serve_api.py

環境變數只在這個檔案裡讀。底下的模組一律靠注入拿到相依，這樣才測得動。

    SLIDEBOX_API_HOST        監聽位址，預設 127.0.0.1
    SLIDEBOX_API_PORT        監聽埠，預設 8800
    SLIDEBOX_API_KEY         設了就強制 X-API-Key；沒設則不驗（區網自用）
    SLIDEBOX_API_OUTPUT_DIR  成品落點，預設 ~/slidebox_api_output
    SLIDEBOX_MODEL           Ollama 模型，預設同桌面 app
    SLIDEBOX_OLLAMA_HOST     Ollama 位址，預設 http://localhost:11434
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from fastapi import FastAPI

from slidebox.composition import build_usecase
from slidebox.domain.entities import Settings
from slidebox_api.app import create_app
from slidebox_api.jobs import JobStore
from slidebox_api.runner import JobWorker, SlideboxExecutor

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


def build() -> FastAPI:
    settings = build_settings()
    store = JobStore()
    # use case 只建一次：它會連帶建立語音辨識器，模型載入要 40 秒並佔著
    # VRAM，每個工作重建一次等於每次重付。
    executor = SlideboxExecutor(build_usecase(settings), settings)
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
    if host not in LOOPBACK and not os.environ.get("SLIDEBOX_API_KEY"):
        # 不擋，只吵：使用者可能真的只在信任的區網裡跑。但「綁在對外介面
        # 又沒有任何驗證」值得在啟動時說一次，而不是等出事才發現。
        print("⚠ 監聽在非 loopback 位址但沒有設 SLIDEBOX_API_KEY："
              "任何連得到這個埠的人都能佔用你的 GPU。", file=sys.stderr)
    uvicorn.run(app, host=host, port=int(os.environ.get("SLIDEBOX_API_PORT",
                                                        str(DEFAULT_PORT))))
