"""WSGI 進入點。真的要對外服務時才會被載入——設定檢查放在這裡。"""
import os

from django.conf import settings
from django.core.wsgi import get_wsgi_application

from newsroom.guards import check_serving_config

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "newsroom.settings")
application = get_wsgi_application()

# 放在 application 之後：要先讓 Django 完成設定載入才讀得到 settings。
check_serving_config(settings.SECRET_KEY, settings.DEBUG)
