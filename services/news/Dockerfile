# 新聞後端（Django）。api 與 scheduler 兩個服務共用這一個映像，只差啟動指令。
# 多架構的基底映像：同一份 Dockerfile 在桌機（amd64）與 Raspberry Pi（arm64）都能建。
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# 先只複製相依清單：程式碼改了不必重裝套件
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# 資料庫與截圖放在 /data，由 compose 掛 volume——放在映像裡的話重建就沒了
ENV PATH="/app/.venv/bin:$PATH" \
    DJANGO_DB_PATH=/data/db.sqlite3 \
    DJANGO_MEDIA_ROOT=/data/media

EXPOSE 8000
# --insecure 讓 DEBUG=False 下的 /admin/ 還有 CSS（與 README 的 systemd 作法相同）
CMD ["sh", "-c", "python manage.py migrate --noinput && exec python manage.py runserver 0.0.0.0:8000 --noreload --insecure"]
