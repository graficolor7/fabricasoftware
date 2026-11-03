"""Excepciones de la fábrica."""


class FactoryError(Exception):
    """Error de uso o de configuración (no gasta tokens)."""


class SupervisorUnavailable(Exception):
    """El proveedor no respondió: red, 5xx, 429, timeout o configuración (401/403/404)."""

    def __init__(self, message: str, retryable: bool = True, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class InvalidSupervisorResponse(Exception):
    """El proveedor respondió, pero no con un veredicto utilizable."""


class LimitExceeded(Exception):
    """Se excedió un límite de coste/contexto: STOP y HUMAN_REVIEW_REQUIRED."""
