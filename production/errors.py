"""领域错误：携带稳定 code，HTTP 层据此映射状态码。"""


class DomainError(Exception):
    """所有可预期的业务规则违反。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class ConflictState(DomainError):
    """对象当前状态不允许该操作（如未汇合审批、授权已撤销）。"""

    code = "conflict_state"
    http_status = 409


class VersionConflict(DomainError):
    """乐观并发：调用方基于过期剧本版本改稿。"""

    code = "version_conflict"
    http_status = 409


class SchedulingConflict(DomainError):
    """场景/演员/团队档期冲突，details 给出逐条原因。"""

    code = "scheduling_conflict"
    http_status = 409

    def __init__(self, message, reasons):
        super().__init__(message, {"reasons": reasons})
        self.reasons = reasons


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class IntegrityGuard(DomainError):
    """唯一性/幂等约束被数据库拒绝时转译的业务错误。"""

    code = "integrity_guard"
    http_status = 409
