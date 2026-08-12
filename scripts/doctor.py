"""Repository-local entry point for the read-only runtime doctor."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_dubbing_runtime.doctor import main


if __name__ == "__main__":
    raise SystemExit(main())
