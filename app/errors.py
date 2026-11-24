"""Domain errors: the one family services raise and the HTTP API answers with.

A ``DomainError`` says what was refused in three parts: ``code``, the stable
kind of refusal; ``message_key`` and ``message_params``, a catalog message
(ADR-0006). ``str()`` is that message in English, for logs and Job messages.
Services raise them and know nothing about HTTP: the single handler in
``app.main`` maps ``code`` to a status (``STATUS``) and translates the message
for the request.

A service's own error class adds the builtin it always was (``ValueError``,
``LookupError``, ``RuntimeError``), so callers that catch the builtin keep
working.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .i18n import DEFAULT_LOCALE, has_message, translate


class DomainError(Exception):
    """A refusal the caller is told about; subclasses fix its ``code``."""

    code = "conflict"
    message_key = ""
    message_params: Mapping[str, Any] = {}
    # Response headers the refusal carries (``Retry-After``), if any.
    headers: Mapping[str, str] | None = None

    def __init__(self, message_key: str, **params: Any) -> None:
        super().__init__(translate(message_key, locale=DEFAULT_LOCALE, **params))
        self.message_key = message_key
        self.message_params = params

    def describe(self, message_key: str, *, fallback: str | None = None,
                 code: str | None = None, **params: Any) -> None:
        """Give the message of an error that keeps its own ``str()``.

        For errors their service names by a short reason: ``message_key`` is
        the reason's catalog message, ``fallback`` the one for a reason that
        has none, ``code`` the reason's kind when it is not the class's.
        """
        if fallback is not None and not has_message(message_key):
            message_key = fallback
        self.message_key = message_key
        self.message_params = params
        if code is not None:
            self.code = code


class Invalid(DomainError):
    """The request names something that cannot be: a bad value or selection."""

    code = "invalid"


class NotFound(DomainError):
    """What the request names does not exist (or not in this Vault)."""

    code = "not_found"


class MethodNotAllowed(DomainError):
    """The address exists, but not for the method of the request."""

    code = "method_not_allowed"

    def __init__(self, message_key: str, *, allow: Iterable[str]) -> None:
        super().__init__(message_key)
        self.headers = {"Allow": ", ".join(allow)}


class Conflict(DomainError):
    """The request is well formed, but the current state does not allow it."""

    code = "conflict"


class Unauthenticated(DomainError):
    """No valid Session, or wrong credentials."""

    code = "unauthenticated"


class Forbidden(DomainError):
    """The caller may not do this from here."""

    code = "forbidden"


class ReauthRequired(DomainError):
    """A fresh Reauthentication is needed; the frontend keys on this code."""

    code = "reauth_required"

    def __init__(self) -> None:
        super().__init__("api.reauth_required")


class RateLimited(DomainError):
    """Too many attempts; ``retry_after`` seconds until the next one."""

    code = "rate_limited"

    def __init__(self, message_key: str, *, retry_after: int) -> None:
        super().__init__(message_key)
        self.headers = {"Retry-After": str(retry_after)}


class Unavailable(DomainError):
    """Something FrostVault needs is not available right now."""

    code = "unavailable"


class UpstreamFailed(DomainError):
    """The storage provider did not answer what was asked."""

    code = "upstream_failed"


class Failed(DomainError):
    """The operation was attempted and failed on the server."""

    code = "failed"


class BadRequest(DomainError):
    """The request cannot be handled as sent."""

    code = "bad_request"


# The HTTP status of each ``code``: the whole vocabulary of the API's refusals.
STATUS: Mapping[str, int] = {
    "bad_request": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "reauth_required": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "invalid": 422,
    "rate_limited": 429,
    "failed": 500,
    "upstream_failed": 502,
    "unavailable": 503,
}


class VaultNotFound(NotFound, LookupError):
    """The Vault of the request does not exist."""

    def __init__(self, message_key: str = "api.vault_not_found") -> None:
        super().__init__(message_key)
