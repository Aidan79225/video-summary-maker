from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.urls import path, re_path
from django.utils.cache import patch_cache_control
from django.views.static import serve

from articles.api import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]

if settings.SERVE_MEDIA:
    # 截圖要直接給瀏覽器讀。
    #
    # 刻意不用 django.conf.urls.static.static()：它的第一件事就是
    # `if not settings.DEBUG: return []`，而本專案的 DEBUG 預設是 False——
    # 照 README 部署的結果會是每一張截圖都 404，而且四處文件都宣稱相反。
    #
    # django.views.static.serve 有 safe_join 擋路徑穿越，而 MEDIA_ROOT 裡
    # 只有本專案自己寫進去的截圖，沒有使用者上傳，風險可接受。前面有
    # nginx 時把 DJANGO_SERVE_MEDIA 設成 False 讓 nginx 服務會更快。
    def _serve_media(request, path):
        # 在請求當下才讀 MEDIA_ROOT，而不是把值烤進 URLConf：後者在測試
        # （override_settings）與任何延後設定的情境下都會指到錯的地方。
        response = serve(request, path, document_root=settings.MEDIA_ROOT)
        # 對外的流量真正碰到 Django 的就是圖片（tunnel 把 media/* 轉到這裡）。
        # 同一個網址的截圖永遠不會變（重跑會換資料夾），所以可以放心讓
        # 瀏覽器與 Cloudflare 邊緣快取。只標 200：304 與 404 不該被快取一天。
        if response.status_code == 200 and settings.MEDIA_CACHE_SECONDS > 0:
            patch_cache_control(response, public=True,
                                max_age=settings.MEDIA_CACHE_SECONDS, immutable=True)
        return response

    urlpatterns += [re_path(r"^media/(?P<path>.*)$", _serve_media)]
