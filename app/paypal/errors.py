"""Typed PayPal errors, so callers can react to *kinds* of failure, not status codes."""

from __future__ import annotations

from typing import Any


class PayPalError(Exception):
    """Base class. `debug_id` is PayPal's trace id; quote it when asking PayPal support."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        name: str | None = None,
        details: list[dict[str, Any]] | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.name = name
        self.details = details or []
        self.debug_id = debug_id

    def user_message(self) -> str:
        return f"PayPal said: {self.message}" + (
            f" (debug id {self.debug_id})" if self.debug_id else ""
        )


class PayPalUnavailable(PayPalError):
    """Network failure or timeout: we never got an answer."""

    retryable = True


class PayPalAuthError(PayPalError):
    """401/403: wrong client id/secret, or the app lacks the Invoicing/Payouts feature."""


class PayPalValidationError(PayPalError):
    """400/422: PayPal rejected the payload. `details` says which field and why."""


class PayPalNotFound(PayPalError):
    pass


class PayPalConflict(PayPalError):
    """409 or a duplicate sender_batch_id: the request was already processed."""


class PayPalRateLimited(PayPalError):
    retryable = True


class PayPalServerError(PayPalError):
    retryable = True


def error_for_status(status: int) -> type[PayPalError]:
    if status in (401, 403):
        return PayPalAuthError
    if status in (400, 422):
        return PayPalValidationError
    if status == 404:
        return PayPalNotFound
    if status == 409:
        return PayPalConflict
    if status == 429:
        return PayPalRateLimited
    if status >= 500:
        return PayPalServerError
    return PayPalError
