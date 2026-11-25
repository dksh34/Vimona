"""Structured domain exception hierarchy for Vimona (`L6`)."""

from __future__ import annotations


class VimonaError(Exception):
    """Base error for all Vimona domain failures."""

    def __init__(self, message: str, *, code: str = "vimona_error", recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


class IsolationError(VimonaError):
    """Raised when network/filesystem isolation or verified process containment fails (`A2`)."""

    def __init__(self, message: str, *, code: str = "isolation_error", recoverable: bool = False) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class StorageError(VimonaError):
    """Raised on local state corruption, locking timeout, or path boundary violations (`I1`–`I9`)."""

    def __init__(self, message: str, *, code: str = "storage_error", recoverable: bool = True) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class ModelDownloadError(VimonaError):
    """Raised on HTTP failure, checksum mismatch, disk shortage, or incompatible model format (`C1`–`C12`)."""

    def __init__(self, message: str, *, code: str = "model_download_error", recoverable: bool = True) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class HardwareProfileError(VimonaError):
    """Raised when hardware probing encounters fatal inconsistencies."""

    def __init__(self, message: str, *, code: str = "hardware_profile_error", recoverable: bool = True) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class HandoffError(VimonaError):
    """Raised when exclusive engine handoff / process lifecycle control fails (`E1`–`E12`)."""

    def __init__(self, message: str, *, code: str = "handoff_error", recoverable: bool = True) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class InsufficientMemoryError(HandoffError):
    """Raised when available system RAM is genuinely below safe engine launch threshold (`E12`)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="insufficient_memory", recoverable=True)


class SkillValidationError(VimonaError):
    """Raised on malformed, unsafe, or version-incompatible `.vimona-skill` inputs (`J1`–`J11`)."""

    def __init__(self, message: str, *, code: str = "skill_validation_error", recoverable: bool = True) -> None:
        super().__init__(message, code=code, recoverable=recoverable)


class PreflightError(VimonaError):
    """Raised when critical startup dependencies (OS/GTK/display) prevent safe UI launch (`M1`–`M9`)."""

    def __init__(self, message: str, *, code: str = "preflight_error", recoverable: bool = False) -> None:
        super().__init__(message, code=code, recoverable=recoverable)
