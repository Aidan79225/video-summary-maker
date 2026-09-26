"""查核流程：用假的 GPU 客戶端，不碰網路。"""
from __future__ import annotations

from datetime import date

from django.test import TestCase

from articles.gpu_client import GpuApiError, JobFailed
from articles.models import Article, ArticleStatus
from factchecks.models import Claim, FactCheckRun, ReviewStatus, RunStatus
from factchecks.runner import (
    MAX_ATTEMPTS,
    ReviewedMeanwhile,
    candidates,
    check_article,
    check_articles,
    save_claims,
)

EVIDENCE = {"source": "law", "title": "醫療法 第一百零六條（2026-05-08 修正版）",
            "official_url": "https://law.moj.gov.tw/Law/LawSearchResult.aspx?ty=ONEBAR&kw=x",
            "api_url": "https://ly.govapi.tw/v2/law_contents?x=1",
            "excerpt": "違反第二十四條第二項規定者，處新臺幣三萬元以上五萬元以下罰鍰。"}


def _claim(verdict="supported", **overrides):
    item = {"quote": "他的行政罰鍰從現行的3萬到5萬", "timestamp": 32.0, "kind": "law_article",
            "statement": "醫療法現行罰鍰為 3 萬到 5 萬元", "subject": {"law": "醫療法"},
            "verdict": verdict, "method": "numeric", "rationale": "相同",
            "evidence": [EVIDENCE]}
    item.update(overrides)
    return item


def _payload(*claims):
    return {"model": "qwen3.5:9b", "claims": list(claims) or [_claim()]}


class FakeClient:
    def __init__(self, result=None, error=None, known_jobs=None, while_waiting=None):
        self.result = result if result is not None else _payload()
        self.error = error
        self.known_jobs = known_jobs or {}
        self.submitted = []
        # 模擬等 GPU 的那幾分鐘裡，別人在後台做了什麼
        self.while_waiting = while_waiting

    def job(self, job_id):
        return self.known_jobs.get(job_id)

    def submit_factcheck(self, source_url, speaker, day, meeting, transcript_text):
        self.submitted.append((source_url, speaker, day, transcript_text))
        if isinstance(self.error, GpuApiError):
            raise self.error
        return "fc-1"

    def wait(self, job_id, timeout, poll_seconds=3.0, on_progress=None):
        if self.while_waiting is not None:
            self.while_waiting()
        if self.error is not None:
            raise self.error
        return self.result


def _article(ivod_id="171140", transcript="00:32 他的行政罰鍰從現行的3萬到5萬",
             status=ArticleStatus.READY):
    return Article.objects.create(
        ivod_id=ivod_id, slug=f"2026-08-25-{ivod_id}", title="t", speaker="邱慧洳",
        meeting="院會", date=date(2026, 8, 25),
        ivod_url=f"https://ivod.ly.gov.tw/Play/Clip/1M/{ivod_id}",
        transcript_text=transcript, status=status)


class SaveTests(TestCase):
    def test_claims_and_evidence_are_saved(self):
        article = _article()
        FactCheckRun.objects.create(article=article)
        saved = save_claims(article, _payload())
        self.assertEqual((saved.total, saved.pending), (1, 0))
        claim = article.claims.get()
        self.assertEqual(claim.review_status, ReviewStatus.AUTO)
        self.assertEqual(claim.evidence.get().excerpt, EVIDENCE["excerpt"])
        run = FactCheckRun.objects.get(article=article)
        self.assertEqual(run.status, RunStatus.DONE)
        self.assertEqual(run.model, "qwen3.5:9b")
        self.assertIsNotNone(run.checked_at)

    def test_contradictions_wait_for_a_human(self):
        article = _article()
        saved = save_claims(article, _payload(_claim("contradicted")))
        self.assertEqual(saved.pending, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.PENDING)

    def test_unknown_verdicts_and_kinds_are_dropped(self):
        article = _article()
        saved = save_claims(article, _payload(_claim("lying"), _claim(kind="opinion"), _claim()))
        self.assertEqual(saved.total, 1)
        self.assertEqual(article.claims.get().index, 1)

    def test_unsafe_urls_are_blanked(self):
        """證據網址會變成網站上可點的連結；javascript: 就是一個點擊型 XSS。"""
        article = _article()
        evidence = {**EVIDENCE, "official_url": "javascript:alert(1)"}
        save_claims(article, _payload(_claim(evidence=[evidence])))
        self.assertEqual(article.claims.get().evidence.get().official_url, "")

    def test_saving_again_replaces_the_previous_claims(self):
        article = _article()
        save_claims(article, _payload(_claim(), _claim()))
        save_claims(article, _payload(_claim()))
        self.assertEqual(article.claims.count(), 1)


class CheckTests(TestCase):
    def test_a_ready_article_is_checked(self):
        article = _article()
        client = FakeClient()
        report = check_articles(client, limit=10, timeout=60)
        self.assertEqual((report.checked, report.claims), (1, 1))
        self.assertEqual(client.submitted[0][1], "邱慧洳")
        self.assertEqual(article.claims.count(), 1)

    def test_articles_that_are_not_ready_or_have_no_transcript_are_skipped(self):
        _article("1", status=ArticleStatus.PENDING)
        _article("2", transcript="")
        self.assertEqual(candidates(limit=10, timeout=60), [])

    def test_a_done_article_is_not_checked_again(self):
        article = _article()
        check_articles(FakeClient(), limit=10, timeout=60)
        self.assertNotIn(article, candidates(limit=10, timeout=60))

    def test_a_service_outage_stops_the_round(self):
        _article("1")
        _article("2")
        report = check_articles(FakeClient(error=GpuApiError("down")), limit=10, timeout=60)
        self.assertEqual(report.failed, 1)
        self.assertEqual(FactCheckRun.objects.filter(status=RunStatus.FAILED).count(), 1)

    def test_a_single_failure_does_not_stop_the_round(self):
        _article("1")
        _article("2")
        report = check_articles(FakeClient(error=JobFailed("bad")), limit=10, timeout=60)
        self.assertEqual(report.failed, 2)

    def test_attempts_are_capped(self):
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.FAILED,
                                    attempts=MAX_ATTEMPTS)
        self.assertEqual(candidates(limit=10, timeout=60), [])

    def test_an_existing_job_is_resumed_instead_of_resubmitted(self):
        """等待途中斷線時，GPU 那邊的工作可能已經跑完；重送等於把成果丟掉。"""
        article = _article()
        FactCheckRun.objects.create(article=article, status=RunStatus.FAILED,
                                    gpu_job_id="fc-old")
        client = FakeClient(known_jobs={"fc-old": {"id": "fc-old", "status": "done"}})
        check_articles(client, limit=10, timeout=60)
        self.assertEqual(client.submitted, [])
        self.assertEqual(article.claims.count(), 1)

    def test_human_reviews_are_never_washed_away(self):
        """攔的 bug：重跑整批換掉主張，審核過的「不符」與駁回紀錄會一起消失。"""
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))
        Claim.objects.update(review_status=ReviewStatus.APPROVED)
        report = check_article(article, FakeClient(), timeout=60)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.APPROVED)

    def test_force_rechecks_even_after_review(self):
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))
        Claim.objects.update(review_status=ReviewStatus.APPROVED)
        report = check_article(article, FakeClient(), timeout=60, force=True)
        self.assertEqual(report.checked, 1)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.AUTO)

    def test_a_review_made_while_waiting_is_not_washed_away(self):
        """攔的 bug：審核檢查在等 GPU 之前，等的那幾分鐘裡有人核准，存檔時照樣整批刪掉。"""
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))

        def approve():
            Claim.objects.update(review_status=ReviewStatus.APPROVED)

        report = check_article(article, FakeClient(while_waiting=approve), timeout=60)
        self.assertEqual((report.skipped, report.checked), (1, 0))
        claim = article.claims.get()
        self.assertEqual(claim.review_status, ReviewStatus.APPROVED)
        self.assertEqual(claim.verdict, "contradicted")
        self.assertEqual(FactCheckRun.objects.get(article=article).status, RunStatus.DONE)

    def test_save_refuses_to_replace_reviewed_claims_unless_forced(self):
        article = _article()
        save_claims(article, _payload(_claim("contradicted")))
        Claim.objects.update(review_status=ReviewStatus.REJECTED)
        with self.assertRaises(ReviewedMeanwhile):
            save_claims(article, _payload(_claim()))
        self.assertEqual(article.claims.get().review_status, ReviewStatus.REJECTED)
        save_claims(article, _payload(_claim()), force=True)
        self.assertEqual(article.claims.get().review_status, ReviewStatus.AUTO)

    def test_a_model_outage_does_not_burn_the_backlog(self):
        """攔的 bug：Ollama 掛掉時每篇都秒失敗，三輪就把整批積壓的嘗試次數燒完、永久放棄。"""
        _article("1")
        _article("2")
        client = FakeClient(error=JobFailed("ModelUnavailable: 連不上 Ollama"))
        report = check_articles(client, limit=10, timeout=60)
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(report.failed, 1)
        run = FactCheckRun.objects.get()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.attempts, 0)
        self.assertIn("ModelUnavailable", run.error)
