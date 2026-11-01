"""配置校验测试（M1 验收：配置错误必须在启动时立刻报清晰错误）。

每条 case 都断言「报错信息里能看出是哪个字段、错在哪」，而不是只断言抛了异常。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from digest.config import ConfigError, load_config  # noqa: E402

BASE = """\
version: 1
global:
  timezone: Asia/Shanghai
  items_per_column: 5
columns:
  - id: trend
    name: 热门
    source: github_trending
    sort: trending_order
  - id: search
    name: 搜索
    source: github_search
    query: "stars:>100"
"""


class RealConfigTest(unittest.TestCase):
    def test_repo_config_loads(self) -> None:
        cfg = load_config(REPO_ROOT / "config" / "columns.yaml")
        self.assertEqual(cfg.version, 1)
        self.assertEqual(len(cfg.enabled_columns), 5)
        self.assertEqual(cfg.items_per_column(), 5)

    def test_trending_uses_two_day_dedupe(self) -> None:
        cfg = load_config(REPO_ROOT / "config" / "columns.yaml")
        self.assertEqual(cfg.columns_by_id()["trending"].dedupe_days, 2)

    def test_column_keywords_reach_filters(self) -> None:
        cfg = load_config(REPO_ROOT / "config" / "columns.yaml")
        f = cfg.filters_for(cfg.columns_by_id()["freebies"])
        self.assertIn("破解", f.exclude_keywords)

    def test_disabled_column_skipped(self) -> None:
        cfg = load_config(REPO_ROOT / "config" / "columns.yaml")
        self.assertNotIn("finance", [c.id for c in cfg.enabled_columns])


class ConfigErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _load(self, text: str):
        p = Path(self.tmp.name) / "columns.yaml"
        p.write_text(text, encoding="utf-8")
        return load_config(p)

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            load_config(Path(self.tmp.name) / "nope.yaml")
        self.assertIn("找不到配置文件", str(cm.exception))

    def test_bad_yaml_reports_line(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            self._load("version: 1\nglobal:\n  timezone: [unclosed\n")
        self.assertIn("YAML 语法错误", str(cm.exception))

    def test_top_level_not_mapping(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            self._load("- just\n- a\n- list\n")
        self.assertIn("顶层必须是映射", str(cm.exception))

    def test_typo_in_global_key(self) -> None:
        text = BASE.replace("items_per_column: 5", "item_per_column: 5")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        msg = str(cm.exception)
        self.assertIn("未知配置项", msg)
        self.assertIn("items_per_column", msg)  # 提示里给出正确写法

    def test_unknown_column_key(self) -> None:
        text = BASE.replace('    query: "stars:>100"', '    query: "stars:>100"\n    sortt: stars')
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        msg = str(cm.exception)
        self.assertIn("未知配置项", msg)
        self.assertIn("columns[1]", msg)

    def test_missing_global_section(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            self._load("version: 1\ncolumns:\n  - id: a\n    name: A\n    source: github_trending\n")
        self.assertIn("缺少必填项", str(cm.exception))

    def test_search_without_query(self) -> None:
        text = BASE.replace('    query: "stars:>100"\n', "")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("必须提供 query", str(cm.exception))

    def test_trending_with_query(self) -> None:
        text = BASE.replace(
            "    sort: trending_order",
            '    sort: trending_order\n    query: "stars:>1"',
        )
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("不接受 query", str(cm.exception))

    def test_trending_wrong_sort(self) -> None:
        text = BASE.replace("    sort: trending_order", "    sort: stars")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("trending_order", str(cm.exception))

    def test_invalid_literal(self) -> None:
        text = BASE.replace("    source: github_search", "    source: github_apiv9")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("取值不在允许范围内", str(cm.exception))

    def test_duplicate_ids(self) -> None:
        text = BASE.replace("  - id: search", "  - id: trend")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("栏目 id 重复", str(cm.exception))

    def test_items_per_column_out_of_range(self) -> None:
        text = BASE.replace("items_per_column: 5", "items_per_column: 99")
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("数值太大", str(cm.exception))

    def test_all_columns_disabled(self) -> None:
        text = BASE.replace("    source: github_trending", "    enabled: false\n    source: github_trending").replace(
            "    source: github_search", "    enabled: false\n    source: github_search"
        )
        with self.assertRaises(ConfigError) as cm:
            self._load(text)
        self.assertIn("至少要有 1 个", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
