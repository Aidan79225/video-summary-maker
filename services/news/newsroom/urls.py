from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from articles.api import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]

if settings.SERVE_MEDIA:
    # 截圖要直接給瀏覽器讀。DEBUG=False 時 Django 預設不服務 media，
    # 所以這裡明確掛上去（見 settings.SERVE_MEDIA 的說明）。
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
