"""FrameExtract Studio voice-dubbing runtime contracts.

The package keeps storage, capability discovery and worker protocols separate
from ML libraries.  Engine-specific model code runs behind subprocess adapters.
"""

from .capabilities import EngineRegistry
from .profiles import VoiceProfileManager

__all__ = ["EngineRegistry", "VoiceProfileManager"]
__version__ = "0.3.0"
