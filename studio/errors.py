"""领域错误层次。"""


class DomainError(Exception):
    """所有业务规则违反的基类。"""

    code = "domain_error"

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    """输入不合法（空值、时间颠倒、枚举越界等）。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    code = "not_found"


class StateError(DomainError):
    """实体当前状态不允许该操作。"""

    code = "state_error"


class ConflictError(DomainError):
    """并发冲突或业务约束冲突（档期、版本、审批等）。"""

    code = "conflict"
