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

# admin 的靜態檔在建置時收進映像，執行時由 WhiteNoise 服務。
# collectstatic 不碰資料庫，所以這裡不需要 /data。
RUN python manage.py collectstatic --noinput

EXPOSE 8000
# gunicorn 而不是 runserver：runserver 沒有 worker 管理，卡住的請求不會被回收。
# Pi 只有四顆核心而且 SQLite 單一寫入者，2 個 worker × 2 條 thread 夠用；
# 逾時 30 秒是給 /media 讀檔的餘裕。scheduler 容器另外用 manage.py 啟動。
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn newsroom.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 30 --access-logfile - --access-logformat '%(h)s %(m)s %(U)s %(s)s %(B)s %(M)sms'"]
