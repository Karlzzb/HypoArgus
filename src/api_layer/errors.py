"""HTTP 控制面错误码（T-04·PRD §9）。

全量错误码枚举 + HTTP 状态映射 + 统一异常载体。错误码是 ``/api/agent/run`` 的对外契约
（与 WS / 前端共享词汇），故集中一处定义、单一来源：

- ``LOCK_EXIST``：重复提交 / 未处理断点（``session_locks`` 行未过期 / 活跃 ``pause_meta`` 下又发 fresh query）。
- ``PAUSE_EXPIRED``：断点 30min 超时（``pause_meta.pause_time`` 超 TTL）。
- ``GRAPH_TIMEOUT``：全局超时（默认 120s，``asyncio.wait_for`` 兜底）。
- ``PARAM_ERROR``：``query`` 与 ``human_response`` 互斥违例 / resume 无对应 ``pause_meta`` 等。
- ``FORBIDDEN``：跨用户访问会话（``session_owner`` 不匹配）。
- ``SESSION_LIMIT``：活跃会话数达上限（默认 100）且无法淘汰。

惰性清理（T-04 范围）：请求路径上命中过期即返回 ``PAUSE_EXPIRED`` / ``LOCK_EXIST``；
后台 sweep 扫孤儿锁 / pause_meta 属 T-08 运维加固。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ErrorCode", "ApiError", "ERROR_HTTP_STATUS", "PAUSE_TTL_SECONDS"]


class ErrorCode(StrEnum):
    """``/api/agent/run`` 全量错误码（PRD §9.2 / §9.3 / §9.7）。"""

    LOCK_EXIST = "LOCK_EXIST"
    PAUSE_EXPIRED = "PAUSE_EXPIRED"
    GRAPH_TIMEOUT = "GRAPH_TIMEOUT"
    PARAM_ERROR = "PARAM_ERROR"
    FORBIDDEN = "FORBIDDEN"
    SESSION_LIMIT = "SESSION_LIMIT"


#: 断点 TTL：``pause_meta`` 超 30min 视为过期（PRD §9.3）。
PAUSE_TTL_SECONDS: int = 30 * 60


#: 错误码 → HTTP 状态映射。``4xx`` 表客户端态错（重试需改请求）、``504`` 表网关超时。
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PARAM_ERROR: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.LOCK_EXIST: 409,
    ErrorCode.PAUSE_EXPIRED: 410,
    ErrorCode.SESSION_LIMIT: 429,
    ErrorCode.GRAPH_TIMEOUT: 504,
}


class ApiError(Exception):
    """控制面统一异常：错误码 + 人类可读 message。路由层捕获后按 :data:`ERROR_HTTP_STATUS`
    映射成 HTTP 响应。``code`` 是机器契约、``message`` 是人读补充。"""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code: ErrorCode = code
        self.message: str = message or code.value

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"
