"""Lazy, injectable operating-system clock-correction backends."""

from .base import BaseTimeSync, normalize_sync_servers
from .command_runner import CommandRunner
from .factory import create_time_sync_backend, detect_time_sync_privilege


__all__ = [
    "BaseTimeSync",
    "CommandRunner",
    "create_time_sync_backend",
    "detect_time_sync_privilege",
    "normalize_sync_servers",
]
