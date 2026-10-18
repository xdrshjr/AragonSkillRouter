"""worker 入口（`python -m skillrouter_api.worker`）。"""

from __future__ import annotations

import asyncio
import sys

from .runner import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)
