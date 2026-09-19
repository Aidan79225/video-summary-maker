"""Django 設定。部署目標是 Raspberry Pi，所以一切從簡：SQLite、無外部服務。"""
from __future__ import annotations

import os
from pathlib import Path

from newsroom.guards import DEV_SECRET_KEY

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes")


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# 正式部署請務必設 DJANGO_SECRET_KEY。這個預設值只夠開發用——它寫在公開
# 的原始碼裡，所以 DEBUG=False 時 wsgi/asgi 會直接拒絕啟動（見 guards.py）。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", DEV_SECRET_KEY)
DEBUG = _env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "articles",
    "factchecks",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # admin 的 CSS／JS 由 WhiteNoise 從 STATIC_ROOT 服務，gunicorn 底下不必再靠
    # runserver 的 --insecure。要放在 SecurityMiddleware 之後、其他之前。
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "newsroom.urls"
WSGI_APPLICATION = "newsroom.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {
            # Pi 的 SD 卡很慢，而排程與 API 會同時讀寫。
            "timeout": 20,
            # 沒有 IMMEDIATE 的話，「先 SELECT 再寫」的交易要到寫入那一刻
            # 才升鎖，此時另一個寫入者已經進來就直接 database is locked，
            # 連 timeout 都不會等。
            "transaction_mode": "IMMEDIATE",
            # WAL 讓讀不會被寫擋住：排程在跑的時候，網站還是要能出得來。
            "init_command": "PRAGMA journal_mode=WAL;",
        },
    }
}

LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# 開發時直接從各 app 的 static 目錄找，不必先 collectstatic；正式環境
# （容器建置時已經 collectstatic）走 STATIC_ROOT，一次掃描、之後從記憶體出。
WHITENOISE_USE_FINDERS = DEBUG
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # 壓縮但不用 manifest：manifest 版本在少了 collectstatic 時整個 admin 會 500，
    # 而這裡的靜態檔只有 admin 自己的，快取失效交給 max-age 就夠。
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 匯入一輪要跑一到兩小時。沒有這段設定的話，維運者盯著的是一個完全沉默的
# 終端機，分不出「還在跑」與「卡住了」。
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "articles": {
            "handlers": ["console"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# --- 本專案自己的設定 ---

# GPU 主機上的摘要 API（slidebox）。Pi 上不跑任何模型，只透過 HTTP 說話。
GPU_API_BASE = os.environ.get("GPU_API_BASE", "http://localhost:8800")
GPU_API_KEY = os.environ.get("GPU_API_KEY", "")
# 一支影片要 2～5 分鐘，詳細模式更久。這是「等一個工作跑完」的上限。
GPU_JOB_TIMEOUT_SECONDS = int(os.environ.get("GPU_JOB_TIMEOUT_SECONDS", "1800"))

# 立法院開放資料
IVOD_API_BASE = os.environ.get("IVOD_API_BASE", "https://ly.govapi.tw/v2/ivods")

# 每天最多處理幾段發言。一段約 3～5 分鐘，預設 20 段約 1～2 小時。
INGEST_DAILY_LIMIT = int(os.environ.get("INGEST_DAILY_LIMIT", "20"))
# 排程每天幾點跑（24 小時制，台北時間）。立法院的逐字稿不是即時產生的，
# 所以隔天凌晨抓前一天的內容。
INGEST_HOUR = int(os.environ.get("INGEST_HOUR", "4"))

# 由 Django 直接服務 /media。正式環境用 nginx 會更好，但 Pi 自用時
# 少一個元件就少一個會壞的東西。
SERVE_MEDIA = _env_bool("DJANGO_SERVE_MEDIA", True)
# 截圖的快取秒數。每次重跑都寫進新的子資料夾，網址一定跟著換，所以同一個
# 網址的內容永遠不會變——放到 Cloudflare 邊緣快取一天，Pi 就幾乎不再被
# 圖片打到。
MEDIA_CACHE_SECONDS = int(os.environ.get("DJANGO_MEDIA_CACHE_SECONDS", str(24 * 3600)))
