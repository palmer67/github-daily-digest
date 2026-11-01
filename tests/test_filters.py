"""质量过滤规则的单元测试（M1 验收：过滤规则 ≥3 case，这里覆盖全部 6 条）。

全部是纯函数，不联网、不碰文件，所以直接构造 Candidate 断言即可。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.filters import (  # noqa: E402
    REASON_AWESOME_NAME,
    REASON_AWESOME_TOPIC,
    REASON_DUPLICATE,
    REASON_KEYWORD_BLACKLIST,
    REASON_NO_DESCRIPTION,
    REASON_NO_LANGUAGE,
    REASON_README_TOO_SHORT,
    REASON_STAR_FORK_RATIO,
    Filters,
    describe_reason,
    is_similar,
    normalize_description,
)
from digest.models import Candidate  # noqa: E402


def cand(**kw) -> Candidate:
    base = dict(
        full_name="owner/good-repo",
        source="github_search",
        description="A tiny useful CLI that does one thing well.",
        language="Python",
        stars=1200,
        forks=320,
    )
    base.update(kw)
    return Candidate(**base)


class MetadataRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Filters()

    def test_good_candidate_passes(self) -> None:
        self.assertIsNone(self.f.check_metadata(cand()))

    def test_missing_description_dropped(self) -> None:
        self.assertEqual(self.f.check_metadata(cand(description="")), REASON_NO_DESCRIPTION)
        self.assertEqual(self.f.check_metadata(cand(description="   ")), REASON_NO_DESCRIPTION)

    def test_missing_language_dropped(self) -> None:
        self.assertEqual(self.f.check_metadata(cand(language=None)), REASON_NO_LANGUAGE)

    def test_awesome_in_name_dropped(self) -> None:
        self.assertEqual(self.f.check_metadata(cand(full_name="sindresorhus/awesome")), REASON_AWESOME_NAME)

    def test_list_suffix_dropped(self) -> None:
        self.assertEqual(
            self.f.check_metadata(cand(full_name="EbookFoundation/free-programming-books-list")),
            REASON_AWESOME_NAME,
        )

    def test_cheatsheet_dropped(self) -> None:
        self.assertEqual(self.f.check_metadata(cand(full_name="someone/python-cheatsheet")), REASON_AWESOME_NAME)

    def test_star_fork_ratio_too_high_dropped(self) -> None:
        # 25000/500 = 50 > 20
        self.assertEqual(
            self.f.check_metadata(cand(stars=25000, forks=500)),
            REASON_STAR_FORK_RATIO,
        )

    def test_zero_forks_does_not_trigger_ratio(self) -> None:
        # forks=0 时比值无意义，不能因此误杀真实小项目
        self.assertIsNone(self.f.check_metadata(cand(stars=8000, forks=0)))

    def test_keyword_blacklist_from_column(self) -> None:
        f = Filters(exclude_keywords=("破解", "crack"))
        self.assertEqual(
            f.check_metadata(cand(description="免费破解版工具下载")),
            REASON_KEYWORD_BLACKLIST,
        )
        self.assertIsNone(f.check_metadata(cand(description="A productivity CLI")))

    def test_can_switch_rules_off(self) -> None:
        f = Filters(require_description=False, require_language=False)
        self.assertIsNone(f.check_metadata(cand(description="", language=None)))


class EnrichedRulesTest(unittest.TestCase):
    def test_awesome_topic_dropped(self) -> None:
        f = Filters()
        self.assertEqual(f.check_enriched(cand(topics=["awesome-list", "python"])), REASON_AWESOME_TOPIC)

    def test_plain_topics_pass(self) -> None:
        f = Filters()
        self.assertIsNone(f.check_enriched(cand(topics=["cli", "python", "tool"])))

    def test_topic_match_is_case_insensitive(self) -> None:
        self.assertEqual(Filters().check_enriched(cand(topics=["Awesome"])), REASON_AWESOME_TOPIC)


class ReadmeRulesTest(unittest.TestCase):
    def test_short_readme_dropped(self) -> None:
        self.assertEqual(Filters().check_readme(cand(readme_chars=400)), REASON_README_TOO_SHORT)

    def test_long_readme_passes(self) -> None:
        self.assertIsNone(Filters().check_readme(cand(readme_chars=12000)))

    def test_unknown_readme_is_not_a_failure(self) -> None:
        # 没拉到 README 记 readme_missing，不算「不合格」（M2 再降级处理）
        self.assertIsNone(Filters().check_readme(cand(readme_chars=None)))


class SimilarDedupeTest(unittest.TestCase):
    """同批近似重复：刷量仓库的简介是逐字复制的，只留第一条。"""

    @staticmethod
    def spam(owner: str) -> Candidate:
        return cand(
            full_name=f"{owner}/ai-api-gateway-free-tier",
            stars=60,
            forks=2,
            description=(
                "AI API gateway / relay: 307 models behind one OpenAI-compatible key. "
                "image2.5 $0.0085/image, Seedance 2.0 Mini"
            ),
        )

    def test_identical_descriptions_kept_once(self) -> None:
        items = [self.spam("apimartntu3sj4aj"), self.spam("apimartbfu0iej"), self.spam("apimartop9jtsd")]
        kept, dropped = Filters().dedupe_similar(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 2)
        self.assertEqual(kept[0].full_name, "apimartntu3sj4aj/ai-api-gateway-free-tier")

    def test_punctuation_and_case_ignored(self) -> None:
        a = cand(full_name="x/a", description="AI API Gateway: 307 models, one OpenAI key, zero markup!")
        b = cand(full_name="y/b", description="ai-api-gateway — 307 models one openai key zero markup")
        kept, dropped = Filters().dedupe_similar([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)

    def test_different_descriptions_both_kept(self) -> None:
        items = [
            cand(full_name="x/a", description="A tiny command-line tool that renames photos by date"),
            cand(full_name="y/b", description="Postgres extension for time-series rollups with compression"),
        ]
        kept, dropped = Filters().dedupe_similar(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_disabled_keeps_everything(self) -> None:
        items = [self.spam("apimartntu3sj4aj"), self.spam("apimartbfu0iej")]
        kept, dropped = Filters(duplicate_similarity=0).dedupe_similar(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_short_descriptions_not_compared(self) -> None:
        # 简介太短（<40 字符）时相似度没有意义，宁可都留着
        items = [cand(full_name="x/a", description="fast cli"), cand(full_name="y/b", description="fast cli")]
        kept, dropped = Filters().dedupe_similar(items)
        self.assertEqual(len(kept), 2)

    def test_order_preserved_first_wins(self) -> None:
        items = [self.spam("z/first"), self.spam("a/second")]
        kept, _ = Filters().dedupe_similar(items)
        self.assertEqual(kept[0].full_name, "z/first/ai-api-gateway-free-tier")


class NormalizeTest(unittest.TestCase):
    def test_normalize_strips_punctuation(self) -> None:
        self.assertEqual(normalize_description("AI-API Gateway!"), "ai api gateway")

    def test_is_similar_threshold(self) -> None:
        a = normalize_description("Postgres extension for time-series rollups")
        b = normalize_description("Postgres extension for time series rollups")
        self.assertTrue(is_similar(a, b, 0.9, 40))
        self.assertFalse(is_similar(a, normalize_description("A rust web server"), 0.9, 40))


class DescribeReasonTest(unittest.TestCase):
    def test_reasons_have_chinese_text(self) -> None:
        for reason in (
            REASON_NO_DESCRIPTION,
            REASON_NO_LANGUAGE,
            REASON_AWESOME_NAME,
            REASON_AWESOME_TOPIC,
            REASON_STAR_FORK_RATIO,
            REASON_README_TOO_SHORT,
            REASON_KEYWORD_BLACKLIST,
            REASON_DUPLICATE,
        ):
            self.assertNotEqual(describe_reason(reason), reason)

    def test_unknown_reason_passthrough(self) -> None:
        self.assertEqual(describe_reason("weird"), "weird")


if __name__ == "__main__":
    unittest.main()
