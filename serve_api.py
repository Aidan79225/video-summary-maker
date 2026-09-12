#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摘要 API 進入點：跑在有 GPU 與 Ollama 的那台機器上。

    uv run --group api serve_api.py

環境變數：
    SLIDEBOX_API_HOST        監聽位址，預設 0.0.0.0（Pi 要連得到）
    SLIDEBOX_API_PORT        監聽埠，預設 8800
    SLIDEBOX_API_KEY         設了就強制 X-API-Key；沒設則不驗（區網自用）
    SLIDEBOX_API_OUTPUT_DIR  成品落點，預設 ~/slidebox_api_output
    SLIDEBOX_MODEL           Ollama 模型，預設同桌面 app
    SLIDEBOX_OLLAMA_HOST     Ollama 位址，預設 http://localhost:11434
"""
import os
import sys

# 讓 src 版面配置可被 import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from slidebox_api.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("SLIDEBOX_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("SLIDEBOX_API_PORT", "8800")),
    )
