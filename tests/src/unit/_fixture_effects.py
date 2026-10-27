"""Deterministic pause and after-effect hooks for native owner stand-ins."""

from typing import Any


async def notify_effect(self: Any) -> None:
    self.started.set()
    if self.block:
        await self.release.wait()
    if self.after:
        self.after()
