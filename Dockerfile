# 摘要 API（serve_api.py），跑在 GPU 主機上。
# 模型不在這個映像裡：Ollama 是另一個容器（見 compose.gpu.yaml），這裡只透過 HTTP 連它。
# 語音辨識（faster-whisper）走 CPU，模型檔快取在 /root/.cache/huggingface 這個 volume。
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
RUN uv sync --frozen --no-dev --group api --no-install-package pyside6

COPY serve_api.py ./
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    SLIDEBOX_API_HOST=0.0.0.0 \
    SLIDEBOX_API_OUTPUT_DIR=/data/output

EXPOSE 8800
CMD ["python", "serve_api.py"]
