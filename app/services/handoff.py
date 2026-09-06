"""Compatibility exports for the owner-thread application navigator.

The old implementation stored Playwright handles in a manager and closed
them from HTTP threads.  The implementation now lives in
``app.services.navigator``; this module keeps the historical import path
without reintroducing that ownership model.
"""
from __future__ import annotations

from app.services.navigator import (
    ApplicationNavigator,
    DuplicateSessionError,
    HeadedSessionManager,
    HeadedSessionWorker,
    NavigatorError,
    NavigatorShutdownError,
    SessionCommandRejected,
    SessionNotFoundError,
)

__all__ = [
    "ApplicationNavigator",
    "DuplicateSessionError",
    "HeadedSessionManager",
    "HeadedSessionWorker",
    "NavigatorError",
    "NavigatorShutdownError",
    "SessionCommandRejected",
    "SessionNotFoundError",
]
