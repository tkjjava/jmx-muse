"""启动入口。"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("MUSE_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("MUSE_BRIDGE_PORT", "8788"))
    uvicorn.run("muse_bridge.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
