"""Cookies News Cockpit: a local-first personal news dashboard."""

from ._version import __version__ as __version__
from .api import create_app

__all__ = ["__version__", "create_app"]
