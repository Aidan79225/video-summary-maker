"""對外服務前的設定檢查。"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from newsroom.guards import DEV_SECRET_KEY, check_serving_config


class GuardTests(SimpleTestCase):
    def test_serving_with_the_public_default_key_is_refused(self):
        """攔的 bug：這串預設值寫在公開的 repo 裡。用它簽 session 與 CSRF
        等於沒簽，而且不會有任何症狀——靜悄悄地就上線了。"""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            check_serving_config(DEV_SECRET_KEY, debug=False)
        self.assertIn("DJANGO_SECRET_KEY", str(ctx.exception))

    def test_a_real_key_passes(self):
        check_serving_config("a-long-random-string-from-secrets-token-urlsafe", False)

    def test_development_is_left_alone(self):
        """開發時用預設值是正常的；DEBUG=True 本來就不該對外。"""
        check_serving_config(DEV_SECRET_KEY, debug=True)
