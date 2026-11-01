"""去重与补位规则的单元测试（M1 验收：补位规则 ≥3 case）。

对应策划书 7.3：
  - 7 天内推过的要跳过（榜单栏目 2 天）
  - 不足 5 条时从同一查询的候选池向下顺延
  - 顺延也凑不够就照实缺口，绝不拿历史/别的内容凑数
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.models import Candidate  # noqa: E402
from digest.state import apply_dedupe, backfill, is_within_window, parse_date  # noqa: E402

TODAY = date(2026, 11, 1)


def c(name: str) -> Candidate:
    return Candidate(full_name=name, source="github_search", description="d", language="Python")


class ParseDateTest(unittest.TestCase):
    def test_date_only(self) -> None:
        self.assertEqual(parse_date("2026-10-31"), date(2026, 10, 31))

    def test_iso_with_z(self) -> None:
        self.assertEqual(parse_date("2026-10-31T12:00:00Z"), date(2026, 10, 31))

    def test_none_and_garbage(self) -> None:
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("not-a-date"))


class WindowTest(unittest.TestCase):
    def test_yesterday_in_window(self) -> None:
        self.assertTrue(is_within_window(TODAY - timedelta(days=1), TODAY, 7))

    def test_six_days_ago_still_in_window(self) -> None:
        self.assertTrue(is_within_window(TODAY - timedelta(days=6), TODAY, 7))

    def test_seven_days_ago_out_of_window(self) -> None:
        # 7 天整的可以再次出现（策划书 M4 验收原文）
        self.assertFalse(is_within_window(TODAY - timedelta(days=7), TODAY, 7))

    def test_future_date_ignored(self) -> None:
        self.assertFalse(is_within_window(TODAY + timedelta(days=1), TODAY, 7))


class DedupeTest(unittest.TestCase):
    def test_drops_recent_only(self) -> None:
        items = [c("a/one"), c("a/two"), c("a/three")]
        sent = {"a/one": "2026-10-31", "a/two": "2026-10-01"}
        fresh, skipped = apply_dedupe(items, sent, TODAY, 7)
        self.assertEqual([x.full_name for x in skipped], ["a/one"])
        self.assertEqual([x.full_name for x in fresh], ["a/two", "a/three"])

    def test_keeps_order(self) -> None:
        items = [c("z/last"), c("a/first")]
        fresh, _ = apply_dedupe(items, {}, TODAY, 7)
        self.assertEqual([x.full_name for x in fresh], ["z/last", "a/first"])

    def test_empty_state_keeps_everything(self) -> None:
        items = [c("a/one"), c("a/two")]
        fresh, skipped = apply_dedupe(items, {}, TODAY, 7)
        self.assertEqual(len(fresh), 2)
        self.assertEqual(skipped, [])

    def test_two_day_window_for_trending(self) -> None:
        items = [c("a/one"), c("a/two")]
        sent = {"a/one": "2026-10-31", "a/two": "2026-10-29"}
        fresh, skipped = apply_dedupe(items, sent, TODAY, 2)
        self.assertEqual([x.full_name for x in skipped], ["a/one"])
        self.assertEqual([x.full_name for x in fresh], ["a/two"])


class BackfillTest(unittest.TestCase):
    def test_fills_from_pool(self) -> None:
        kept = [c("a/1"), c("a/2"), c("a/3")]
        pool = [c("a/4"), c("a/5"), c("a/6")]
        items, shortfall = backfill(kept, pool, 5)
        self.assertEqual([x.full_name for x in items], ["a/1", "a/2", "a/3", "a/4", "a/5"])
        self.assertEqual(shortfall, 0)

    def test_shortfall_when_pool_runs_out(self) -> None:
        kept = [c("a/1"), c("a/2")]
        pool = [c("a/3")]
        items, shortfall = backfill(kept, pool, 5)
        self.assertEqual(len(items), 3)
        self.assertEqual(shortfall, 2)  # 必填：照实缺口，不拿历史凑数

    def test_no_pool_at_all(self) -> None:
        items, shortfall = backfill([c("a/1")], [], 5)
        self.assertEqual(len(items), 1)
        self.assertEqual(shortfall, 4)

    def test_does_not_overshoot(self) -> None:
        kept = [c(f"a/{i}") for i in range(5)]
        pool = [c("a/extra")]
        items, shortfall = backfill(kept, pool, 5)
        self.assertEqual(len(items), 5)
        self.assertEqual(shortfall, 0)


if __name__ == "__main__":
    unittest.main()
