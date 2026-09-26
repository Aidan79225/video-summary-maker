"""每日排程：查核要等準確率量過、明確打開才自動跑。"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase, override_settings

from articles.management.commands import run_scheduler


class DailyJobTests(SimpleTestCase):
    def _run(self) -> list[str]:
        with mock.patch.object(run_scheduler, "call_command") as call:
            run_scheduler.daily_job(days=1)
        return [c.args[0] for c in call.call_args_list]

    @override_settings(FACTCHECK_ENABLED=False)
    def test_fact_checks_do_not_run_until_enabled(self):
        """規格要求上線前先量準確率；沒打開就自動發佈查核結果，等於跳過這一步。"""
        self.assertEqual(self._run(), ["ingest_ivod"])

    @override_settings(FACTCHECK_ENABLED=True)
    def test_fact_checks_follow_the_ingest_once_enabled(self):
        self.assertEqual(self._run(), ["ingest_ivod", "factcheck_articles"])

    @override_settings(FACTCHECK_ENABLED=True)
    def test_a_failed_ingest_still_lets_the_fact_checks_run(self):
        with mock.patch.object(run_scheduler, "call_command",
                               side_effect=[RuntimeError("down"), None]) as call:
            run_scheduler.daily_job(days=1)
        self.assertEqual(call.call_count, 2)
