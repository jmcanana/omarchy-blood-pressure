"""Write one command file for the sidecar. The JSON arrives as one argv entry; no shell parses it."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(sys.argv[2], encoding="utf-8")
    os.chmod(path, 0o600)


if __name__ == "__main__":
    main()
