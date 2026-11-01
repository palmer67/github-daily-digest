"""消息切分边界测试（M1 验收：切分边界 ≥3 case）。

背景（A-7 实测）：企业微信 markdown 精确上限 4096 字节，4358 字节会报 errcode 40058。
所以按 4000 字节切，且只能在卡片边界 `\\n### ` 上切，不能把一张卡劈开。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.renderer import CARD_SEP, nbytes, split_markdown  # noqa: E402


def card(title: str, body_chars: int) -> str:
    return f"{CARD_SEP}{title}\n" + "正" * body_chars  # 中文 3 字节/字，方便撑爆上限


class NbytesTest(unittest.TestCase):
    def test_ascii(self) -> None:
        self.assertEqual(nbytes("abc"), 3)

    def test_chinese_is_three_bytes(self) -> None:
        self.assertEqual(nbytes("速递"), 6)


class SplitTest(unittest.TestCase):
    def test_small_text_untouched(self) -> None:
        text = f"# 标题\n{CARD_SEP}卡片1\n短内容"
        self.assertEqual(split_markdown(text), [text])

    def test_exactly_at_limit_untouched(self) -> None:
        text = "a" * 4000
        self.assertEqual(split_markdown(text), [text])

    def test_two_cards_split_onto_boundary(self) -> None:
        # 两张卡各约 3005 字节，合计超 3500 → 必须切成 2 块，每块只含完整卡片
        text = card("一", 1000) + card("二", 1000)
        chunks = split_markdown(text, limit_bytes=3500)
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(nbytes(chunk), 3500)
            self.assertIn(CARD_SEP, chunk)  # 每块都从卡片边界开始（不是半张卡）
        self.assertEqual("".join(chunks), text)  # 不丢字

    def test_no_card_is_cut_in_half(self) -> None:
        text = card("卡片一", 500) + card("卡片二", 500) + card("卡片三", 500)
        chunks = split_markdown(text, limit_bytes=1600)
        self.assertGreater(len(chunks), 1)
        # 所有卡片标题都完整保留，且各出现一次
        joined = "".join(chunks)
        for title in ("卡片一", "卡片二", "卡片三"):
            self.assertEqual(joined.count(title), 1)
        # 每个块里的卡片数量是整数张：块内的卡片边界数 = 卡片数
        for chunk in chunks:
            self.assertTrue(chunk.startswith(CARD_SEP))

    def test_oversized_single_card_returned_alone(self) -> None:
        text = f"# 头\n{CARD_SEP}巨卡\n" + "长" * 5000
        chunks = split_markdown(text, limit_bytes=4000)
        self.assertTrue(any(nbytes(c) > 4000 for c in chunks))  # 原样返回，交给上层告警
        self.assertEqual("".join(chunks), text)

    def test_many_cards_all_chunks_within_limit(self) -> None:
        text = "".join(card(f"卡{i}", 400) for i in range(30))
        chunks = split_markdown(text, limit_bytes=4000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(nbytes(chunk), 4000)
        self.assertEqual("".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
