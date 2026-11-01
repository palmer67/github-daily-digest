"""采集器的解析测试：不联网，用假 client 喂固定 HTML / JSON。

为什么值得测：trending 是纯 HTML 正则解析，页面一改版就静默解析成 0 条——
这是策划书第 10 节点名的一期最大单点风险，用固定样本把当前正则钉住。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.collectors import search, trending  # noqa: E402
from digest.collectors.base import CollectorError  # noqa: E402
from digest.config import ColumnCfg  # noqa: E402

TRENDING_HTML = """
<div class="Box">
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/openai/whisper" data-view-component="true">openai / whisper</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">
      Robust Speech Recognition via Large-Scale Weak Supervision
  </p>
  <div>
    <span itemprop="programmingLanguage">Python</span>
    <span>1,234 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/rustdesk/rustdesk">rustdesk / rustdesk</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">
      An open-source remote desktop, designed to work out of the box
  </p>
  <div>
    <span itemprop="programmingLanguage">Rust</span>
    <span>987 stars today</span>
  </div>
</article>
</div>
"""


class FakeClient:
    def __init__(self, text: str = "", payload: dict | None = None) -> None:
        self.text = text
        self.payload = payload or {}
        self.calls: list[str] = []

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        return self.text

    def search_repositories(self, query: str, sort: str = "stars", order: str = "desc", per_page: int = 40) -> dict:
        self.calls.append(f"search:{query}|{sort}|{per_page}")
        return self.payload


def column(**kw) -> ColumnCfg:
    base = dict(id="t", name="T", source="github_search", query="stars:>1")
    base.update(kw)
    return ColumnCfg(**base)


class TrendingTest(unittest.TestCase):
    def test_parses_two_cards(self) -> None:
        items = trending.collect(FakeClient(TRENDING_HTML), column(source="github_trending", sort="trending_order", query=None))
        self.assertEqual([c.full_name for c in items], ["openai/whisper", "rustdesk/rustdesk"])
        self.assertEqual(items[0].language, "Python")
        self.assertEqual(items[0].stars_today, 1234)
        self.assertEqual(items[1].stars_today, 987)
        self.assertIn("Robust Speech Recognition", items[0].description)

    def test_requests_daily_since(self) -> None:
        client = FakeClient(TRENDING_HTML)
        trending.collect(client, column(source="github_trending", sort="trending_order", query=None, since="weekly"))
        self.assertEqual(client.calls, ["https://github.com/trending?since=weekly"])

    def test_zero_parsed_raises(self) -> None:
        # 页面改版时必须炸，不许静默推空栏目（策划书第 10 节）
        with self.assertRaises(CollectorError):
            trending.collect(FakeClient("<html>nothing here</html>"), column(source="github_trending", sort="trending_order", query=None))


class SearchTest(unittest.TestCase):
    def test_expands_date_placeholder(self) -> None:
        q = search.expand_query("created:>{14d} stars:>200", date(2026, 11, 1))
        self.assertEqual(q, "created:>2026-10-18 stars:>200")

    def test_keeps_query_without_placeholder(self) -> None:
        self.assertEqual(search.expand_query("stars:>1", date(2026, 11, 1)), "stars:>1")

    def test_to_candidate_maps_fields(self) -> None:
        item = {
            "full_name": "a/b",
            "description": "  desc  ",
            "language": "Go",
            "stargazers_count": 321,
            "forks_count": 12,
            "topics": ["cli"],
            "pushed_at": "2026-10-30T00:00:00Z",
            "html_url": "https://github.com/a/b",
        }
        c = search.to_candidate(item, "github_search")
        self.assertEqual((c.full_name, c.description, c.language, c.stars, c.forks), ("a/b", "desc", "Go", 321, 12))
        self.assertEqual(c.topics, ["cli"])

    def test_topic_source_prefixes_query(self) -> None:
        client = FakeClient(payload={"items": []})
        with self.assertRaises(CollectorError):
            search.collect(client, column(source="github_topic", query="dsh-plugin"), date(2026, 11, 1))
        self.assertEqual(client.calls, ["search:topic:dsh-plugin|stars|40"])

    def test_empty_result_raises_with_query(self) -> None:
        with self.assertRaises(CollectorError) as cm:
            search.collect(FakeClient(payload={"items": []}), column(), date(2026, 11, 1))
        self.assertIn("stars:>1", str(cm.exception))

    def test_uses_pool_as_per_page(self) -> None:
        client = FakeClient(payload={"items": [{"full_name": "a/b"}]})
        items = search.collect(client, column(pool=60), date(2026, 11, 1))
        self.assertEqual(client.calls, ["search:stars:>1|stars|60"])
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
