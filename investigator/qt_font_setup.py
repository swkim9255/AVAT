"""Select a variable-font-capable Windows backend before QApplication starts."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def configure_text_rendering(arguments: Sequence[str] = ()) -> None:
    """Avoid the legacy GDI path for bundled Noto/Material variable fonts.

    Qt 6.7 supports variable application fonts on Windows through DirectWrite
    (or FreeType), not GDI. Newer Qt releases also accept this explicit choice.
    Preserve intentional platform/font-engine overrides, including offscreen
    test runs; this changes only the current application's environment.
    """
    if sys.platform != "win32":
        return
    if any(argument == "-platform" or argument.startswith("-platform=") for argument in arguments):
        return
    platform = os.environ.get("QT_QPA_PLATFORM", "windows")
    options = platform.split(":")
    if options[0].casefold() != "windows" or ";" in platform:
        return
    if any(option.casefold().startswith("fontengine=") for option in options[1:]):
        return
    os.environ["QT_QPA_PLATFORM"] = f"{platform}:fontengine=directwrite"


__all__ = ["configure_text_rendering"]
