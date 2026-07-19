"""Executable worker role used by the Kubernetes worker Deployment."""

from __future__ import annotations

import asyncio
import signal

from katilim_analiz.config import get_settings
from katilim_analiz.logging import configure_logging
from katilim_analiz.runtime.worker import run_worker


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    await run_worker(settings, stop=stop)


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
