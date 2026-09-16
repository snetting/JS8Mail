"""Entry point for the portable Windows JS8Mail executable."""

from __future__ import annotations

import sys
from pathlib import Path

from js8mail.tools.app import main


def windows_main() -> None:
    """Start with sensible desktop defaults and open the local UI."""
    arguments = list(sys.argv[1:])
    if "--open-browser" not in arguments:
        arguments.append("--open-browser")
    if not any(
        argument == "--database" or argument.startswith("--database=") for argument in arguments
    ):
        executable_dir = Path(sys.executable).resolve().parent
        arguments.extend(("--database", str(executable_dir / "js8mail.sqlite3")))
    sys.argv[1:] = arguments
    main()


if __name__ == "__main__":
    windows_main()
