"""啟動前的設定檢查。

刻意不放在 settings.py 裡：settings 會被測試、`manage.py migrate`、
`ingest_ivod` 等所有指令 import，在那裡 raise 會讓一個「只有對外服務才
危險」的設定擋住所有離線工作。改由 wsgi/asgi 呼叫——那兩個檔案只有真的
在服務 HTTP 請求時才會被載入。
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

# 與 settings.py 共用的預設值。這串字在公開的 repo 裡，等於全世界都知道。
DEV_SECRET_KEY = "dev-only-not-for-production"


def check_serving_config(secret_key: str, debug: bool) -> None:
    """對外服務前的最低要求。不符合就拒絕啟動。

    SECRET_KEY 是 session cookie 與 CSRF token 的簽章金鑰。用公開 repo 裡
    的預設值對外服務，等於任何人都能偽造登入狀態與表單——而這種錯誤不會
    有任何症狀，靜悄悄地就上線了。

    只在 DEBUG=False 時擋：開發時用預設值是正常的，而 DEBUG=True 本來就
    不該對外。
    """
    if not debug and secret_key == DEV_SECRET_KEY:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY 還是預設值，拒絕啟動。\n"
            "這串預設值寫在公開的原始碼裡，用它簽 session 與 CSRF 等於沒簽。\n"
            "請設一個夠長的隨機字串，例如：\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n"
            "然後放進 systemd 的 Environment= 或 shell 的環境變數。"
        )
