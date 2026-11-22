from __future__ import annotations

from dataclasses import dataclass


class EngineError(Exception):
    """A structured, user-facing rule error.

    Every error carries the expression and (when known) the field it belongs
    to so the UI can point at the exact rule instead of failing generically.
    """

    def __init__(self, code, message, *, field=None, expression=None,
                 path=None, details=None, affected=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.expression = expression
        self.path = path
        self.details = details or {}
        self.affected = affected or []

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "expression": self.expression,
            "path": self.path,
            "details": self.details,
            "affected_downstream": self.affected,
        }


def error(code, message, **kwargs):
    return EngineError(code, message, **kwargs)
