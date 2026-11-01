"""M3 验收：卡片渲染、按栏目分段、拆分不劈卡、企业微信 errcode 判定。

M3 验收四条（策划书第 8 节）里，能自动化的三条都在这里：
- 无超长截断（每条消息 ≤ 4000 字节）；
- 故意超长 → 自动拆多条且卡片完整；
- 错误 key 必须判定失败（errcode 93000 → 抛 WeComError，不是「HTTP 200 就算成功」）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.models import Candidate  # noqa: E402
from digest.notifiers.wecom import (  # noqa: E402
    NotifierError,
    WeComError,
    WeComNotifier,
    mask_url,
)
from digest.renderer import (  # noqa: E402
    format_stars,
    nbytes,
    render_card,
    render_messages,
)
from digest.summarizer.schema import Card  # noqa: E402


def cand(name: str = "acme/tool", stars: int = 1234) -> Candidate:
    return Candidate(
        full_name=name,
        source="test",
        description="把 CSV 转成 JSON 的小工具",
        language="Python",
        stars=stars,
        forks=10,
        topics=["cli"],
        pushed_at="",
        created_at="",
        stars_today=88,
        readme_chars=1200,
        html_url=f"https://github.com/{name}",
    )


def card(*, what: str = "把 CSV 转成 JSON 的小工具", pitfall: str = "", degraded: bool = False) -> Card:
    return Card.model_validate(
        {
            "what": what,
            "value": "适合偶尔要处理表格、又不想装 Excel 的人",
            "cost": {
                "price": "免费",
                "needs_api_key": False,
                "needs_server": False,
                "needs_gpu": False,
            },
            "difficulty": 2,
            "pitfall": pitfall,
            "degraded": degraded,
        }
    )


class FormatStarsTest(unittest.TestCase):
    def test_small(self) -> None:
        self.assertEqual(format_stars(42), "42")

    def test_thousands(self) -> None:
        self.assertEqual(format_stars(1234), "1.2k")

    def test_large(self) -> None:
        self.assertEqual(format_stars(108619), "108.6k")


class RenderCardTest(unittest.TestCase):
    def test_five_sections_and_link(self) -> None:
        text = render_card(1, cand(), card(pitfall="需要 Python 3.12 以上"))
        self.assertTrue(text.startswith("### 1. acme/tool　⭐1.2k · 今日 +88 · Python"))
        for key in ("**是什么**", "**价值**", "**成本**", "**坑**", "[👉 看项目](https://github.com/acme/tool)"):
            self.assertIn(key, text)

    def test_pitfall_omitted_when_empty(self) -> None:
        text = render_card(1, cand(), card(pitfall=""))
        self.assertNotIn("**坑**", text)

    def test_degraded_marked(self) -> None:
        text = render_card(2, cand(), card(degraded=True))
        self.assertIn("自动摘要失败", text)


class RenderMessagesTest(unittest.TestCase):
    def test_one_message_per_short_column(self) -> None:
        result = render_messages(
            "2026-11-01",
            [("trending", "今日热门", [(cand(), card())])],
            limit_bytes=4000,
        )
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].parts, 1)
        self.assertTrue(result.messages[0].text.startswith("## 今日热门（2026-11-01）"))
        self.assertLessEqual(result.messages[0].nbytes, 4000)

    def test_long_column_split_without_breaking_cards(self) -> None:
        long_text = "很长的一段说明" * 40
        items = [(cand(f"acme/tool-{i}"), card(what=long_text)) for i in range(5)]
        result = render_messages("2026-11-01", [("rising", "新兴潜力", items)], limit_bytes=4000)
        self.assertGreater(len(result.messages), 1)
        for m in result.messages:
            self.assertLessEqual(m.nbytes, 4000, m.text[:40])
        for i in range(5):
            hits = [m for m in result.messages if f"acme/tool-{i}　" in m.text]
            self.assertEqual(len(hits), 1, f"tool-{i} 被劈开或丢失")

    def test_continuation_header_counts(self) -> None:
        long_text = "很长的一段说明" * 40
        items = [(cand(f"acme/tool-{i}"), card(what=long_text)) for i in range(5)]
        result = render_messages("2026-11-01", [("rising", "新兴潜力", items)], limit_bytes=4000)
        total = len(result.messages)
        for i, m in enumerate(result.messages, 1):
            if i == 1:
                self.assertIn("## 新兴潜力（2026-11-01）", m.text)
            else:
                self.assertIn(f"续 {i}/{total}", m.text)

    def test_oversized_single_card_warns(self) -> None:
        items = [(cand(), card(what="超" * 2000))]
        result = render_messages("2026-11-01", [("x", "栏目", items)], limit_bytes=4000)
        self.assertTrue(result.warnings)
        self.assertTrue(any("超过" in w for w in result.warnings))

    def test_empty_column_skipped(self) -> None:
        result = render_messages("2026-11-01", [("x", "空栏目", [])], limit_bytes=4000)
        self.assertEqual(result.messages, [])
        self.assertEqual(result.total_bytes, 0)


class FakeTransport:
    def __init__(self, bodies: list[tuple[int, str]]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, bytes, float]] = []

    def __call__(self, url: str, payload: bytes, timeout: float) -> tuple[int, str]:
        self.calls.append((url, payload, timeout))
        return self.bodies.pop(0) if self.bodies else (200, '{"errcode":0,"errmsg":"ok"}')


class WeComNotifierTest(unittest.TestCase):
    URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1ac36cf9-secret-key"

    def notifier(self, transport: FakeTransport, retries: int = 2) -> tuple[WeComNotifier, list[float]]:
        sleeps: list[float] = []
        n = WeComNotifier(self.URL, sleep=sleeps.append, transport=transport, retries=retries)
        return n, sleeps

    def test_success_requires_errcode_zero(self) -> None:
        t = FakeTransport([(200, '{"errcode":0,"errmsg":"ok"}')])
        n, _ = self.notifier(t)
        body = n.send_markdown("hi")
        self.assertEqual(body["errcode"], 0)
        self.assertEqual(n.sent, 1)

    def test_bad_key_http_200_is_failure_and_not_retried(self) -> None:
        t = FakeTransport([(200, '{"errcode":93000,"errmsg":"invalid webhook url"}')])
        n, sleeps = self.notifier(t)
        with self.assertRaises(WeComError) as ctx:
            n.send_markdown("hi")
        self.assertEqual(ctx.exception.errcode, 93000)
        self.assertEqual(len(t.calls), 1, "93000 不该重试")
        self.assertEqual(sleeps, [])
        self.assertEqual(n.sent, 0)

    def test_rate_limit_retries_after_60s(self) -> None:
        t = FakeTransport(
            [(200, '{"errcode":45009,"errmsg":"api freq out of limit"}'), (200, '{"errcode":0,"errmsg":"ok"}')]
        )
        n, sleeps = self.notifier(t)
        n.send_markdown("hi")
        self.assertEqual(sleeps, [60.0])
        self.assertEqual(n.sent, 1)

    def test_too_long_is_fatal(self) -> None:
        t = FakeTransport([(200, '{"errcode":40058,"errmsg":"too long"}')])
        n, _ = self.notifier(t)
        with self.assertRaises(WeComError):
            n.send_markdown("hi")
        self.assertEqual(len(t.calls), 1)

    def test_non_json_body_is_error(self) -> None:
        t = FakeTransport([(502, "<html>bad gateway</html>")])
        n, _ = self.notifier(t, retries=0)
        with self.assertRaises(NotifierError):
            n.send_markdown("hi")

    def test_network_error_retried(self) -> None:
        class Boom:
            def __init__(self) -> None:
                self.n = 0

            def __call__(self, url: str, payload: bytes, timeout: float) -> tuple[int, str]:
                self.n += 1
                if self.n == 1:
                    raise TimeoutError("timed out")
                return (200, '{"errcode":0,"errmsg":"ok"}')

        t = Boom()
        n, sleeps = self.notifier(t)  # type: ignore[arg-type]
        n.send_markdown("hi")
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(n.sent, 1)

    def test_key_masked(self) -> None:
        masked = mask_url(self.URL)
        self.assertNotIn("1ac36cf9-secret-key", masked)
        self.assertIn("key=1ac3***", masked)
        n, _ = self.notifier(FakeTransport([]))
        self.assertNotIn("secret-key", n.masked)

    def test_empty_webhook_rejected(self) -> None:
        with self.assertRaises(NotifierError):
            WeComNotifier("")


if __name__ == "__main__":
    unittest.main()
