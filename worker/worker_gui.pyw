"""双击启动入口：.pyw 由 pythonw.exe 打开，不带命令行窗口。

实现全在 worker_gui.py——从终端 `python worker_gui.py` 运行可看输出调试。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_gui import main  # noqa: E402

main()
