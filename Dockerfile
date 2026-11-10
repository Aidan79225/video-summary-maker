# 摘要 API（serve_api.py），跑在 GPU 主機上。
# 模型不在這個映像裡：Ollama 是另一個容器（見 compose.gpu.yaml），這裡只透過 HTTP 連它。
# 語音辨識（faster-whisper）用顯示卡（compose 給了 gpus: all；沒有 GPU 會自動退回 CPU），
# 模型檔快取在 /root/.cache/huggingface 這個 volume。
# YouTube 多人說話者區分（pyannote.audio，--group diarization）帶進 PyTorch 與它的 CUDA 函式庫，
# 映像會大很多、第一次建置要下載很久；它的模型也快取在同一個 volume，第一次用多人模式時才下載。
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# imageio-ffmpeg 附的 Linux 靜態 ffmpeg 在容器裡讀立法院的 HLS 串流會 segfault，
# 改用 Debian 的套件；imageio-ffmpeg 看到這個環境變數就會直接用它。
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ENV IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

COPY pyproject.toml uv.lock ./
# pyside6 是桌面 app 才需要的；API 走的是不碰 Qt 的 build_usecase，跳過它省下幾百 MB
RUN uv sync --frozen --no-dev --group api --group whisper-gpu --group diarization --no-install-package pyside6
# ctranslate2 在執行時動態載入 cuBLAS／cuDNN；pip 版的函式庫不在系統路徑上，要指給它
ENV LD_LIBRARY_PATH=/app/.venv/lib/python3.13/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib

COPY serve_api.py ./
COPY src ./src

# PYTHONPATH：serve_api.py 自己把 src 加進搜尋路徑，但 `python -m slidebox_api.diarize_check`
# 這種直接叫模組的用法不會，沒有它在容器裡會找不到 slidebox／slidebox_api。
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    SLIDEBOX_API_HOST=0.0.0.0 \
    SLIDEBOX_API_OUTPUT_DIR=/data/output

EXPOSE 8800
CMD ["python", "serve_api.py"]
