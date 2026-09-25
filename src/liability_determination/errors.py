"""责任认定服务向 API 和 CLI 暴露的稳定错误。"""


class LiabilityError(RuntimeError):
    code = "liability_error"
    status = 400


class NotFound(LiabilityError):
    code = "not_found"
    status = 404


class Conflict(LiabilityError):
    code = "conflict"
    status = 409


class Forbidden(LiabilityError):
    code = "forbidden"
    status = 403


class InvalidState(LiabilityError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LiabilityError):
    code = "validation_failed"
    status = 422
