"""Worker 单测的导入路径：worker/ 下的模块是脚本形态（不在任何包里），
pytest 默认只入径 tests/ 与 src/，这里把 worker/ 目录补进来。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
