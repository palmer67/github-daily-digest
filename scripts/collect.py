#!/usr/bin/env python3
"""薄入口：把 src/ 加进 sys.path 后交给 digest.cli。

之所以单独放一个脚本，是为了在 Actions 里能直接写 `python scripts/collect.py`，
不用设置 PYTHONPATH，也不依赖 pip 安装本包。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from digest.cli import main  # noqa: E402  (必须在 sys.path 调整之后导入)

if __name__ == "__main__":
    sys.exit(main())
