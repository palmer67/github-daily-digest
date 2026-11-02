"""守望脚本的测试：今天推没推、告警发不发得出去。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("watchdog", ROOT / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(watchdog)


def _write_state(path: Path, day: str, pushed: bool = True) -> None:
    doc = {
        "version": 1,
        "sent": {"owner/repo": day} if pushed else {},
        "runs": {day: {"pushed": pushed, "messages": 5, "cards": 25, "at": day}} if pushed else {},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


class CheckTest(unittest.TestCase):
    def test_already_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sent.json"
            _write_state(state, "2026-11-02")
            pushed, detail = watchdog.check("2026-11-02", state)
            self.assertTrue(pushed)
            self.assertIn("5 条消息", detail)

    def test_not_pushed_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sent.json"
            _write_state(state, "2026-11-01")
            pushed, detail = watchdog.check("2026-11-02", state)
            self.assertFalse(pushed)
            self.assertIn("还没有 pushed=true", detail)

    def test_missing_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pushed, detail = watchdog.check("2026-11-02", Path(tmp) / "nope.json")
            self.assertFalse(pushed)
            self.assertIn("状态文件不存在", detail)

    def test_broken_state_file_still_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "sent.json"
            state.write_text("{ 这不是 json", encoding="utf-8")
            pushed, detail = watchdog.check("2026-11-02", state)
            self.assertFalse(pushed)
            self.assertIn("状态文件读不出来", detail)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "sent.json"
        patcher = mock.patch.object(watchdog, "DEFAULT_STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_push_today_exits_zero(self) -> None:
        _write_state(self.state, "2026-11-02")
        with mock.patch.object(watchdog.monitor, "today_in_shanghai", return_value="2026-11-02"):
            self.assertEqual(watchdog.main([]), 0)

    def test_missing_push_alerts_and_exits_three(self) -> None:
        _write_state(self.state, "2026-11-01")
        sent: list[str] = []

        class FakeNotifier:
            def __init__(self, url: str) -> None:
                self.url = url

            def send_markdown(self, content: str) -> None:
                sent.append(content)

        with mock.patch.object(watchdog.monitor, "today_in_shanghai", return_value="2026-11-02"):
            with mock.patch.object(watchdog, "WeComNotifier", FakeNotifier):
                with mock.patch.dict("os.environ", {"WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"}, clear=False):
                    self.assertEqual(watchdog.main([]), 3)
        self.assertEqual(len(sent), 1)
        self.assertIn("2026-11-02", sent[0])
        self.assertIn("没跑成", sent[0])
        self.assertNotIn("qyapi", sent[0])
        self.assertNotIn("key=", sent[0])

    def test_dry_run_does_not_send(self) -> None:
        _write_state(self.state, "2026-11-01")
        with mock.patch.object(watchdog.monitor, "today_in_shanghai", return_value="2026-11-02"):
            with mock.patch.object(watchdog, "WeComNotifier") as notifier:
                self.assertEqual(watchdog.main(["--dry-run"]), 3)
        notifier.assert_not_called()

    def test_alert_failure_exits_five(self) -> None:
        _write_state(self.state, "2026-11-01")
        with mock.patch.object(watchdog.monitor, "today_in_shanghai", return_value="2026-11-02"):
            with mock.patch.dict("os.environ", {"WECOM_WEBHOOK_URL": ""}, clear=False):
                self.assertEqual(watchdog.main([]), 5)


if __name__ == "__main__":
    unittest.main()
