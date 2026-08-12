"""Standalone desktop client for :mod:`voice_dubbing_runtime`.

The package root intentionally imports neither Qt nor the runtime's ML stack.
Use ``python -m voice_dubbing_app`` to start the application.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.3.0"
