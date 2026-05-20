"""Public API for score-sort."""

from .core import *  # noqa: F403
from .cli import main, parse_args

__all__ = [name for name in globals() if not name.startswith("_")]
