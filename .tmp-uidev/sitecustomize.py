"""本地 UI 开发环境专用：Windows 下 locale 编码是 GBK，alembic 读带中文注释的
alembic.ini（encoding="locale"）会炸。这里把 alembic 的 configparser 读取固定为
UTF-8。仅用于 .tmp-uidev 的临时 dev 进程，通过 PYTHONPATH 注入，不进仓库运行时。"""

import alembic.util.compat as _compat


def _read_config_parser_utf8(file_config, file_argument):
    return file_config.read(file_argument, encoding="utf-8")


_compat.read_config_parser = _read_config_parser_utf8
