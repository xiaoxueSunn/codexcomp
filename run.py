"""Entry point: uvicorn on 127.0.0.1:8787 (loopback only, auth passthrough)."""
import logging
import os

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

if __name__ == "__main__":
    uvicorn.run(
        "guard.server:app",
        host="127.0.0.1",
        port=int(os.environ.get("GUARD_PORT", "8787")),
        log_level="info",
    )
