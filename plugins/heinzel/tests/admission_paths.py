"""Where the moved admission tests find the package directory (they used to find the tools directory)."""

from pathlib import Path

import steuermaschine_heinzel.native.admission_lib as _lib

TOOLS = Path(_lib.__file__).resolve().parent
