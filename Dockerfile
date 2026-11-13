# 摘要 API（serve_api.py），跑在 GPU 主機上。
# 模型不在這個映像裡：Ollama 是另一個容器（見 compose.gpu.yaml），這裡只透過 HTTP 連它。
# 語音辨識（faster-whisper）用顯示卡（compose 給了 gpus: all；沒有 GPU 會自動退回 CPU），
# 模型檔快取在 /root/.cache/huggingface 這個 volume。
#
# 兩個 Python 環境（原因與細節見 src/slidebox/infrastructure/pyannote_worker.py 的說明）：
#   /app/.venv         主環境：API、議會與單人工作、語音辨識。CUDA 12＋cuDNN 9（ctranslate2 要的），
#                      LD_LIBRARY_PATH 指給它。沒有 torch、沒有 pyannote——和沒有多人模式時完全一樣。
#   /opt/diarize-venv  YouTube 多人說話者區分（pyannote.audio，--only-group diarization）。PyTorch 要
#                      CUDA 13＋cuDNN 9；兩個版本的 cuDNN 輪子都解到同一個資料夾、檔名相同，放在同一個
#                      環境會互相蓋掉。只由 pyannote_worker.py 這個獨立行程使用（SLIDEBOX_DIARIZATION_PYTHON），
#                      啟動它的時候不給 LD_LIBRARY_PATH。映像會大很多、第一次建置要下載很久；
#                      它的模型也快取在同一個 volume，第一次用多人模式時才下載。
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
# 主環境：和沒有多人模式時完全一樣（沒有 --group diarization）。
# pyside6 是桌面 app 才需要的；API 走的是不碰 Qt 的 build_usecase，跳過它省下幾百 MB
RUN uv sync --frozen --no-dev --group api --group whisper-gpu --no-install-package pyside6
# ctranslate2 在執行時動態載入 cuBLAS／cuDNN；pip 版的函式庫不在系統路徑上，要指給它
ENV LD_LIBRARY_PATH=/app/.venv/lib/python3.13/site-packages/nvidia/cublas/lib:/app/.venv/lib/python3.13/site-packages/nvidia/cudnn/lib

# 第二個環境：只有 diarization 群組（--only-group：不裝專案本身與它的相依套件）。
# UV_PROJECT_ENVIRONMENT 把它建在專案資料夾之外：不改的話 uv 會建在 /app/.venv，蓋掉主環境。
RUN UV_PROJECT_ENVIRONMENT=/opt/diarize-venv uv sync --frozen --only-group diarization
# 工作行程用這個 Python 啟動（不給它上面的 LD_LIBRARY_PATH：torch 要的是它自己那套 CUDA 13）
ENV SLIDEBOX_DIARIZATION_PYTHON=/opt/diarize-venv/bin/python

# 建置時真的去看兩個環境裡裝了什麼：主環境沒有 torch 與 CUDA 13、第二個環境沒有 CUDA 12 與主環境的套件、
# LD_LIBRARY_PATH 只指向主環境並找得到 cuDNN 9／cuBLAS 12。不乾淨就讓建置失敗，不要做出一個悄悄壞掉的映像。
COPY docker/check_environments.py /opt/check_environments.py
RUN /app/.venv/bin/python /opt/check_environments.py /app/.venv/bin/python /opt/diarize-venv/bin/python

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
