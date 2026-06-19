from __future__ import annotations


class VariError(Exception):
    pass


class VariAuthError(VariError):
    """401 or otherwise rejected auth context."""


class VariForbiddenError(VariError):
    """403 forbidden (policy/context/IP)."""


class VariCloudflareError(VariError):
    """Cloudflare/WAF HTML challenge instead of JSON."""


class VariUnexpectedResponse(VariError):
    """Non-2xx with body."""

