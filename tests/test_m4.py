"""M4 测试：去重窗口、补位、如实标注、幂等、状态落盘。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.models import Candidate  # noqa: E402
from digest.pipeline import ColumnResult  # noqa: E402
from digest.renderer import render_messages  # noqa: E402
from digest.state import already_pushed, load_state_doc, save_state_doc  # noqa: E402
from digest.summarizer import SummaryResult  # noqa: E402
from digest.summarizer.schema import Card, CostInfo  # noqa: E402

CONFIG = Path(__file__).resolve().parents[1] / "config" / "columns.yaml"


def make_cand(name: str, stars: int = 100) -> Candidate:
    return Candidate(
        full_name=name,
        source="test",
        description=f"{name} 的描述",
        language="Python",
        stars=stars,
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
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_markdown(self, text: str) -> None:
        self.sent.append(text)


class FakeClient:
    """离线用的摘要客户端，只需要被 summarize 不会用到它。"""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0


class RenderingNotesTest(unittest.TestCase):
    def test_note_goes_on_first_part_only(self):
        cands = [(make_cand(f"a/r{i}"), make_summary(make_cand(f"a/r{i}")).card) for i in range(3)]
        res = render_messages("2026-11-01", [("col", "栏目标题", cands)], notes={"col": "今日仅 3 条"})
        self.assertEqual(len(res.messages), 1)
        self.assertIn("## 栏目标题（2026-11-01）　今日仅 3 条", res.messages[0].text)

    def test_other_column_untouched(self):
        cands = [(make_cand("a/r"), make_summary(make_cand("a/r")).card)]
        res = render_messages("2026-11-01", [("c1", "甲", cands), ("c2", "乙", cands)], notes={"c1": "今日仅 1 条"})
        self.assertNotIn("今日仅", res.messages[1].text)


class DailyFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "sent.json"
        self.notifier = FakeNotifier()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _patches(self, kept_per_column: dict[str, int], sent: dict[str, str] | None = None):
        """把采集/README/摘要都换成假的，只保留 daily.py 自己的编排逻辑。"""
        pool: dict[str, list[Candidate]] = {}

        def fake_run_column(client, config, column, sent_map, today, log=print):
            n = kept_per_column.get(column.id, 5)
            need = config.items_per_column(column)
            items = [make_cand(f"{column.id}/item{i}") for i in range(n)]
            shortfall = max(0, need - len(items))
            pool[column.id] = items
            return ColumnResult(
                column=column,
                items=items,
                stats={"kept": len(items), "shortfall": shortfall, "deduped": 0, "backfilled": 0},
            )

        return [
            mock.patch("digest.daily.run_column", side_effect=fake_run_column),
            mock.patch("digest.daily.fetch_readme", return_value=("README 正文", "raw:main/README.md")),
            mock.patch("digest.daily.summarize", side_effect=lambda c, cand, text, *a, **k: make_summary(cand)),
        ]

    def _run(self, day: str, dry_run: bool = False, force: bool = False):
        from digest.daily import run_daily

        return run_daily(
            CONFIG,
            self.state,
            day,
            gh=mock.Mock(),
            client=FakeClient(),
            notifier=self.notifier,
            dry_run=dry_run,
            force=force,
            log=lambda *_: None,
        )

    def test_first_run_pushes_five_messages_and_writes_state(self):
        with self._patches({"trending": 5, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})[0], \
             self._patches({})[1], self._patches({})[2]:
            report = self._run("2026-11-01")
        self.assertFalse(report.skipped)
        self.assertEqual(report.messages, 5)
        self.assertEqual(report.pushed, 5)
        self.assertTrue(report.state_written)
        doc = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(doc["runs"]["2026-11-01"]["pushed"])
        self.assertEqual(len(doc["sent"]), 25)

    def test_second_run_same_day_is_skipped(self):
        patches = self._patches({"trending": 5, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})
        with patches[0], patches[1], patches[2]:
            self._run("2026-11-01")
        before = len(self.notifier.sent)
        with self._patches({})[0] as run_col:
            report = self._run("2026-11-01")
        self.assertTrue(report.skipped)
        self.assertEqual(report.skip_reason, "already_pushed")
        self.assertEqual(len(self.notifier.sent), before, "秒退不能重复推送")
        run_col.assert_not_called()

    def test_seven_day_old_items_can_come_back(self):
        save_state_doc(self.state, {"sent": {"freebies/item0": "2026-10-25"}, "runs": {}})
        patches = self._patches({"freebies": 1})
        with patches[0] as run_col, patches[1], patches[2]:
            self._run("2026-11-01", dry_run=True)
        sent_map = run_col.call_args[0][3]
        self.assertIn("freebies/item0", sent_map, "去重窗口内应该把这条交给 run_column 判定")

    def test_yesterday_item_still_deduped(self):
        save_state_doc(self.state, {"sent": {"freebies/item0": "2026-10-31"}, "runs": {}})
        from digest.state import apply_dedupe

        today = date(2026, 11, 1)
        fresh, skipped = apply_dedupe([make_cand("freebies/item0")], {"freebies/item0": "2026-10-31"}, today, 7)
        self.assertEqual(fresh, [])
        self.assertEqual(len(skipped), 1)

    def test_shortfall_is_annotated_in_message(self):
        patches = self._patches({"trending": 3, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})
        with patches[0], patches[1], patches[2]:
            self._run("2026-11-01")
        joined = "\n".join(self.notifier.sent)
        self.assertIn("今日仅 3 条", joined)
        report = load_state_doc(self.state)
        self.assertEqual(report["runs"]["2026-11-01"]["columns"]["trending"]["shortfall"], 2)

    def test_dry_run_does_not_push_or_write_state(self):
        patches = self._patches({"trending": 5, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})
        with patches[0], patches[1], patches[2]:
            report = self._run("2026-11-01", dry_run=True)
        self.assertEqual(report.pushed, 0)
        self.assertFalse(report.state_written)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.notifier.sent, [])

    def test_force_repushes_same_day(self):
        patches = self._patches({"trending": 5, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})
        with patches[0], patches[1], patches[2]:
            self._run("2026-11-01")
        with self._patches({"trending": 5, "rising": 5, "dsh-ecosystem": 5, "workbench": 5, "freebies": 5})[0], \
             self._patches({})[1], self._patches({})[2]:
            report = self._run("2026-11-01", force=True)
        self.assertFalse(report.skipped)
        self.assertEqual(report.pushed, 5)


class StateDocTest(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = load_state_doc(Path(tmp) / "nope.json")
        self.assertEqual(doc["sent"], {})
        self.assertEqual(doc["runs"], {})
        self.assertFalse(already_pushed(doc, "2026-11-01"))

    def test_old_format_without_runs_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sent.json"
            p.write_text('{"version": 1, "sent": {"a/b": "2026-11-01"}}', encoding="utf-8")
            doc = load_state_doc(p)
        self.assertEqual(doc["sent"], {"a/b": "2026-11-01"})
        self.assertEqual(doc["runs"], {})

    def test_roundtrip_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "sent.json"
            save_state_doc(p, {"sent": {"b/c": "2026-11-01", "a/b": "2026-10-31"}, "runs": {"2026-11-01": {"pushed": True}}})
            doc = load_state_doc(p)
            self.assertEqual(list(doc["sent"]), ["a/b", "b/c"], "写盘时按名字排序，便于看 git diff")
            self.assertTrue(already_pushed(doc, "2026-11-01"))
            self.assertFalse((p.parent / "sent.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
