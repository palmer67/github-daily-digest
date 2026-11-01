"""M5 测试：心跳、告警、DeepSeek 认证失败降级、推送失败不发心跳。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest import monitor  # noqa: E402
from digest.models import Candidate  # noqa: E402
from digest.pipeline import ColumnResult  # noqa: E402
from digest.summarizer import SummaryResult  # noqa: E402
from digest.summarizer.deepseek import LLMAuthError  # noqa: E402
from digest.summarizer.schema import Card, CostInfo, fallback_card  # noqa: E402

CONFIG = Path(__file__).resolve().parents[1] / "config" / "columns.yaml"


def make_cand(name: str) -> Candidate:
    return Candidate(
        full_name=name,
        source="test",
        description=f"{name} 的描述",
        language="Python",
        stars=100,
        forks=5,
        topics=["test"],
        pushed_at="2026-11-01T00:00:00Z",
        html_url=f"https://github.com/{name}",
    )


def make_summary(cand: Candidate) -> SummaryResult:
    card = Card(
        what=f"{cand.full_name} 是测试项目",
        value="测试价值",
        cost=CostInfo(price="免费", needs_api_key=False, needs_server=False, needs_gpu=False),
        difficulty=2,
        pitfall="",
        cost_basis="",
    )
    return SummaryResult(card=card, ok=True)


class FakeNotifier:
    def __init__(self, fail_first: bool = False) -> None:
        self.sent: list[str] = []
        self.fail_first = fail_first

    def send_markdown(self, text: str) -> None:
        if self.fail_first and not self.sent:
            raise RuntimeError("errcode=93000 invalid webhook url")
        self.sent.append(text)


class FakeClient:
    model = "fake"


class MonitorHeartbeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_and_read_heartbeat(self):
        monitor.write_heartbeat(self.dir, "2026-11-01", 5, 25, degraded=False)
        hb = monitor.read_heartbeat(self.dir)
        self.assertEqual(hb["date"], "2026-11-01")
        self.assertEqual(hb["messages"], 5)
        self.assertEqual(hb["cards"], 25)
        self.assertFalse(hb["degraded"])
        self.assertFalse(monitor.heartbeat_missing(self.dir, "2026-11-01"))
        self.assertTrue(monitor.heartbeat_missing(self.dir, "2026-11-02"))

    def test_missing_file_is_missing(self):
        self.assertTrue(monitor.heartbeat_missing(self.dir, "2026-11-01"))
        self.assertEqual(monitor.read_heartbeat(self.dir), {})

    def test_ping_without_url_is_skipped(self):
        with mock.patch.dict("os.environ", {"HEARTBEAT_URL": ""}):
            ok, detail = monitor.ping()
        self.assertTrue(ok)
        self.assertIn("skipped", detail)

    def test_ping_reports_http_status(self):
        resp = mock.MagicMock(status=200)
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            ok, detail = monitor.ping("https://hc-ping.com/abc")
        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 200")

    def test_ping_failure_is_reported_not_raised(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            ok, detail = monitor.ping("https://hc-ping.com/abc")
        self.assertFalse(ok)
        self.assertIn("OSError", detail)

    def test_alert_text_has_no_secret_or_url(self):
        text = monitor.alert_message("2026-11-01", "推送失败", "errcode=93000")
        self.assertIn("2026-11-01", text)
        self.assertIn("推送失败", text)
        self.assertNotIn("qyapi", text)
        self.assertNotIn("key=", text)

    def test_stale_alert_mentions_heartbeat(self):
        text = monitor.stale_alert("2026-11-01", {"date": "2026-10-31", "at": "2026-10-31T07:40:00+0800"})
        self.assertIn("到点没有收到成功心跳", text)
        self.assertIn("2026-10-31", text)


class DegradedAuthTest(unittest.TestCase):
    """DeepSeek key 失效：整批降级成原始简介 + 发告警，且绝不逐张卡重试。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sent.json"
        self.notifier = FakeNotifier()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_auth_error_degrades_once_and_alerts(self):
        calls = {"n": 0}

        def boom(client, cand, text, *a, **k):
            calls["n"] += 1
            raise LLMAuthError(401, "authentication_error")

        def fake_run_column(client, config, column, sent_map, today, log=print):
            items = [make_cand(f"freebies/r{i}") for i in range(5)]
            return ColumnResult(column=column, items=items, stats={"kept": 5, "shortfall": 0, "deduped": 0, "backfilled": 0})

        with mock.patch("digest.daily.run_column", side_effect=fake_run_column), \
             mock.patch("digest.daily.fetch_readme", return_value=("README 正文", "raw")), \
             mock.patch("digest.daily.summarize", side_effect=boom):
            from digest.daily import run_daily

            report = run_daily(
                CONFIG,
                self.state,
                "2026-11-01",
                gh=mock.Mock(),
                client=FakeClient(),
                notifier=self.notifier,
                columns=["freebies"],
                log=lambda *_: None,
            )

        self.assertEqual(calls["n"], 1, "认证失败后不应该对每张卡重试")
        self.assertIn("DeepSeek 认证失败", report.degraded)
        self.assertEqual(report.push_error, None)
        self.assertEqual(report.pushed, 1)
        self.assertEqual(len(self.notifier.sent), 2, "一条正文 + 一条告警")
        self.assertIn("每日开源速递告警", self.notifier.sent[1])
        doc = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(doc["runs"]["2026-11-01"]["degraded"])
        self.assertEqual(report.heartbeat, "skipped")

    def test_fallback_card_marks_degraded_reason(self):
        card = fallback_card(make_cand("a/r"), "DeepSeek 认证失败")
        self.assertTrue(card.degraded)
        self.assertIn("DeepSeek 认证失败", card.degraded_reason)
        self.assertEqual(card.what, "a/r 的描述")


class PushFailureTest(unittest.TestCase):
    """webhook 失效：任务判失败，心跳一个字都不许发。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sent.json"
        self.notifier = FakeNotifier(fail_first=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_push_failure_writes_no_heartbeat(self):
        def fake_run_column(client, config, column, sent_map, today, log=print):
            items = [make_cand(f"freebies/r{i}") for i in range(5)]
            return ColumnResult(column=column, items=items, stats={"kept": 5, "shortfall": 0, "deduped": 0, "backfilled": 0})

        with mock.patch("digest.daily.run_column", side_effect=fake_run_column), \
             mock.patch("digest.daily.fetch_readme", return_value=("README", "raw")), \
             mock.patch("digest.daily.summarize", side_effect=lambda c, cand, text, *a, **k: make_summary(cand)):
            from digest.daily import run_daily

            report = run_daily(
                CONFIG,
                self.state,
                "2026-11-01",
                gh=mock.Mock(),
                client=FakeClient(),
                notifier=self.notifier,
                columns=["freebies"],
                log=lambda *_: None,
            )

        self.assertIsNotNone(report.push_error)
        self.assertEqual(report.pushed, 0)
        self.assertFalse(report.ok)
        self.assertFalse(monitor.heartbeat_path(Path(self.tmp.name)).exists())
        self.assertEqual(report.heartbeat, "")
        doc = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertFalse(doc["runs"]["2026-11-01"]["pushed"])

    def test_successful_run_writes_heartbeat(self):
        def fake_run_column(client, config, column, sent_map, today, log=print):
            items = [make_cand(f"freebies/r{i}") for i in range(5)]
            return ColumnResult(column=column, items=items, stats={"kept": 5, "shortfall": 0, "deduped": 0, "backfilled": 0})

        notifier = FakeNotifier()
        with mock.patch("digest.daily.run_column", side_effect=fake_run_column), \
             mock.patch("digest.daily.fetch_readme", return_value=("README", "raw")), \
             mock.patch("digest.daily.summarize", side_effect=lambda c, cand, text, *a, **k: make_summary(cand)):
            from digest.daily import run_daily

            report = run_daily(
                CONFIG,
                self.state,
                "2026-11-01",
                gh=mock.Mock(),
                client=FakeClient(),
                notifier=notifier,
                columns=["freebies"],
                log=lambda *_: None,
            )

        self.assertEqual(report.push_error, None)
        hb = monitor.read_heartbeat(Path(self.tmp.name))
        self.assertEqual(hb["date"], "2026-11-01")
        self.assertEqual(hb["messages"], 1)
        doc = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(doc["runs"]["2026-11-01"]["pushed"])


if __name__ == "__main__":
    unittest.main()
