"""Storage 层异常类。"""

from __future__ import annotations


class StorageError(Exception):
    """存储后端错误的基类。"""

    def __init__(self, message: str, *, ret: int | None = None) -> None:
        super().__init__(message)
        self.ret = ret


class TokenExpiredError(StorageError):
    """access_token 过期或无效。运营约定：用户去后台重新生成 token。"""


class RecordNotFoundError(StorageError):
    """指定 recordID 在表里不存在。"""


class UnknownColumnError(StorageError):
    """写入时引用了表里不存在的列名。"""
