"""统一错误码与异常处理（§9.1 / 附录 A）。

所有对外错误必须走 ApiError：前端只根据 `code` 分支，不解析 message。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging import get_logger

log = get_logger("skillrouter.errors")


class Code:
    """错误码常量（与 spec 附录 A 一一对应）。"""

    # 认证 / 会话
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    LOGIN_THROTTLED = "LOGIN_THROTTLED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CSRF_INVALID = "CSRF_INVALID"
    UNAUTHENTICATED = "UNAUTHENTICATED"

    # 通用
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DB_BUSY = "DB_BUSY"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    INTERNAL = "INTERNAL"
    MAINTENANCE = "MAINTENANCE"

    # 资产 / 能力
    PROVIDER_IN_USE = "PROVIDER_IN_USE"
    CREDENTIAL_IN_USE = "CREDENTIAL_IN_USE"
    CREDENTIAL_DISABLED = "CREDENTIAL_DISABLED"
    CAPABILITY_IN_USE = "CAPABILITY_IN_USE"
    PARAM_MAP_UNRESOLVED = "PARAM_MAP_UNRESOLVED"
    INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
    UNSUPPORTED_MODE = "UNSUPPORTED_MODE"
    UNSUPPORTED_PARAM = "UNSUPPORTED_PARAM"
    PROVIDER_NOT_AVAILABLE = "PROVIDER_NOT_AVAILABLE"
    CAPABILITY_NOT_CALLABLE = "CAPABILITY_NOT_CALLABLE"
    ASYNC_QUERY_MISSING = "ASYNC_QUERY_MISSING"
    VERSION_IMMUTABLE = "VERSION_IMMUTABLE"
    PUBLISH_EVIDENCE_MISSING = "PUBLISH_EVIDENCE_MISSING"

    # SKILL
    SKILL_DEPENDENCY_MISSING = "SKILL_DEPENDENCY_MISSING"
    SKILL_DISABLED = "SKILL_DISABLED"
    SKILL_IMPORT_INVALID = "SKILL_IMPORT_INVALID"
    SKILL_EXPORT_FAILED = "SKILL_EXPORT_FAILED"

    # 上游 / 执行
    UPSTREAM_AUTH = "UPSTREAM_AUTH"
    UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
    UPSTREAM_QUOTA = "UPSTREAM_QUOTA"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_INVALID_RESPONSE = "UPSTREAM_INVALID_RESPONSE"
    UPSTREAM_BAD_REQUEST = "UPSTREAM_BAD_REQUEST"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    TASK_STATUS_UNKNOWN = "TASK_STATUS_UNKNOWN"
    CANCEL_UNSUPPORTED = "CANCEL_UNSUPPORTED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_TYPE_UNSUPPORTED = "FILE_TYPE_UNSUPPORTED"

    # 访问控制
    TOKEN_SCOPE_DENIED = "TOKEN_SCOPE_DENIED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

    # 引擎
    UPGRADE_IN_PROGRESS = "UPGRADE_IN_PROGRESS"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    ENGINE_SWITCH_FAILED = "ENGINE_SWITCH_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SSRF_BLOCKED = "SSRF_BLOCKED"


#: code -> (http status, 中文默认说明, retryable, hint)
REGISTRY: dict[str, tuple[int, str, bool, str]] = {
    Code.AUTH_INVALID_CREDENTIALS: (401, "用户名或密码错误", False, "检查输入；注意大小写"),
    Code.NOT_INITIALIZED: (409, "平台尚未完成初始化", False, "运行 python scripts/bootstrap.py 完成初始化"),
    Code.LOGIN_THROTTLED: (429, "登录尝试过多", False, "等待 retry_after 秒后重试"),
    Code.PASSWORD_CHANGE_REQUIRED: (403, "必须先修改默认密码", False, "前往 /change-password 完成改密"),
    Code.SESSION_EXPIRED: (401, "会话已过期或被吊销", False, "重新登录"),
    Code.CSRF_INVALID: (403, "CSRF 校验失败", False, "刷新页面后重试"),
    Code.UNAUTHENTICATED: (401, "未登录", False, "请先登录"),
    Code.VALIDATION_FAILED: (400, "请求字段校验失败", False, "按 details.fields 修正后重试"),
    Code.NOT_FOUND: (404, "对象不存在或无权查看", False, "检查标识是否正确"),
    Code.CONFLICT: (409, "操作与当前状态冲突", False, "刷新获取最新状态后重试"),
    Code.DB_BUSY: (503, "数据库写等待超时", True, "稍后重试；若持续发生需降低并发"),
    Code.INVALID_STATE_TRANSITION: (409, "状态机非法迁移", False, "刷新获取最新状态后重试"),
    Code.INTERNAL: (500, "平台内部错误", False, "把 request_id 报告给管理员以便定位"),
    Code.MAINTENANCE: (503, "平台处于维护模式", True, "稍后重试"),
    Code.PROVIDER_IN_USE: (409, "提供商仍有关联凭证或能力", False, "先查看影响面，或使用 ?force=true 软删"),
    Code.CREDENTIAL_IN_USE: (409, "凭证仍被能力路由引用", False, "查看影响面并切换引用后再删除"),
    Code.CREDENTIAL_DISABLED: (409, "凭证已被停用或过期", False, "在凭证页启用或替换凭证"),
    Code.CAPABILITY_IN_USE: (409, "能力被 SKILL/令牌/在途任务引用", False, "查看影响面；改用停用而非删除"),
    Code.PARAM_MAP_UNRESOLVED: (400, "参数映射引用了未声明的来源", False, "检查 param_map 的 input.* / route.* 引用"),
    Code.INPUT_SCHEMA_INVALID: (400, "输入不符合 Schema", False, "按 details.fields 修正"),
    Code.UNSUPPORTED_MODE: (400, "请求的调用模式不被该能力支持", False, "查看能力详情中的 modes"),
    Code.UNSUPPORTED_PARAM: (400, "未声明的参数或不被支持的 MIME", False, "移入 options.extensions 或删除"),
    Code.PROVIDER_NOT_AVAILABLE: (409, "指定的提供商不可用或无可用路由", False, "查看错误体中的可用 provider 列表"),
    Code.CAPABILITY_NOT_CALLABLE: (409, "能力当前不可调用（无可用路由或已停用）", False, "检查凭证状态与路由启用情况"),
    Code.ASYNC_QUERY_MISSING: (400, "异步模式缺少提交或查询配置", False, "补齐 async.submit / async.query"),
    Code.VERSION_IMMUTABLE: (409, "已发布版本不可修改", False, "创建新版本"),
    Code.PUBLISH_EVIDENCE_MISSING: (409, "发布缺少有效的测试证据", False, "先跑一次通过的测试（24 小时内）"),
    Code.SKILL_DEPENDENCY_MISSING: (409, "SKILL 依赖的能力版本缺失或已废弃", False, "补齐依赖或发布新版本"),
    Code.SKILL_DISABLED: (409, "SKILL 已被停用", False, "由管理员在 SKILL 页启用"),
    Code.SKILL_IMPORT_INVALID: (400, "导入内容不合法", False, "按 issues 修正后重试"),
    Code.SKILL_EXPORT_FAILED: (500, "连接包生成失败", True, "重试；若持续失败查看服务日志"),
    Code.UPSTREAM_AUTH: (502, "上游拒绝鉴权", False, "到凭证页检测/替换凭证"),
    Code.UPSTREAM_RATE_LIMIT: (429, "上游限流", True, "降低并发或稍后重试"),
    Code.UPSTREAM_QUOTA: (402, "上游余额或配额不足", False, "到提供商控制台充值"),
    Code.UPSTREAM_TIMEOUT: (504, "上游超时", True, "查看任务状态，勿重复提交"),
    Code.UPSTREAM_UNAVAILABLE: (503, "上游不可用", True, "检查状态页或切换提供商"),
    Code.UPSTREAM_INVALID_RESPONSE: (502, "上游响应无法解析", False, "检查 response_map 与示例响应"),
    Code.UPSTREAM_BAD_REQUEST: (400, "上游拒绝请求参数", False, "按上游错误信息修正参数"),
    Code.UPSTREAM_ERROR: (502, "上游返回错误", False, "查看脱敏后的上游错误摘要"),
    Code.TASK_STATUS_UNKNOWN: (409, "无法判定上游是否已受理", False, "调用对账接口查询"),
    Code.CANCEL_UNSUPPORTED: (409, "上游不支持取消", False, "只能停止本地等待，远端可能仍在执行"),
    Code.IDEMPOTENCY_KEY_REUSED: (409, "同一幂等键对应了不同请求", False, "使用新的幂等键"),
    Code.FILE_TOO_LARGE: (413, "文件超过大小上限", False, "压缩或拆分文件"),
    Code.FILE_TYPE_UNSUPPORTED: (415, "文件类型不在白名单内", False, "转换格式后重试"),
    Code.TOKEN_SCOPE_DENIED: (403, "令牌没有该能力的权限", False, "在管理端扩大作用域"),
    Code.TOKEN_REVOKED: (401, "令牌已被撤销", False, "联系管理员重新签发"),
    Code.TOKEN_EXPIRED: (401, "令牌已过期", False, "联系管理员重新签发"),
    Code.TOKEN_INVALID: (401, "令牌无效", False, "检查令牌是否正确复制"),
    Code.QUOTA_EXCEEDED: (429, "平台配额已用尽", True, "调整配额或等待窗口重置"),
    Code.BUDGET_EXCEEDED: (402, "平台预算已用尽", False, "调整月预算"),
    Code.UPGRADE_IN_PROGRESS: (409, "已有引擎升级正在执行", False, "等待当前升级结束"),
    Code.ENGINE_UNAVAILABLE: (503, "Agent 引擎网关不可用", True, "查看 /settings/engine，检查网关进程"),
    Code.ENGINE_SWITCH_FAILED: (500, "引擎切换失败，已保持/恢复上一版本", False, "查看升级记录中的失败检查项"),
    Code.APPROVAL_REQUIRED: (409, "该操作需要管理员审批", False, "在 Agent 面板批准或拒绝该请求"),
    Code.SSRF_BLOCKED: (400, "目标地址被出站策略拒绝", False, "把地址加入白名单或改用公网地址"),
}


class ApiError(Exception):
    """平台统一异常。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        status: int | None = None,
    ) -> None:
        entry = REGISTRY.get(code, (500, "未知错误", False, ""))
        status_code, default_message, default_retryable, default_hint = (entry + ("",))[:4]
        self.code = code
        self.status = status or status_code
        self.message = message or default_message
        self.hint = hint if hint is not None else default_hint
        self.retryable = default_retryable if retryable is None else retryable
        self.details = details or {}
        super().__init__(self.message)

    def body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "hint": self.hint,
                "retryable": self.retryable,
                "details": self.details,
                "request_id": request_id,
            }
        }


def error_response(exc: ApiError, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=jsonable_encoder(exc.body(request_id)))


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ANN202
        rid = _request_id(request)
        if exc.status >= 500:
            log.error("api_error", extra={"code": exc.code, "rid": rid, "detail": exc.message})
        return error_response(exc, rid)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query"))
            fields.append({"path": loc, "message": err.get("msg", "校验失败")})
        api = ApiError(
            Code.VALIDATION_FAILED,
            details={"fields": fields},
        )
        return error_response(api, _request_id(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = Code.NOT_FOUND if exc.status_code == 404 else (
            Code.UNAUTHENTICATED if exc.status_code == 401 else Code.CONFLICT
        )
        api = ApiError(code, message=str(exc.detail) if exc.detail else None, status=exc.status_code)
        return error_response(api, _request_id(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
        rid = _request_id(request)
        log.exception("unhandled_exception", extra={"rid": rid, "path": request.url.path})
        return error_response(ApiError(Code.INTERNAL), rid)
