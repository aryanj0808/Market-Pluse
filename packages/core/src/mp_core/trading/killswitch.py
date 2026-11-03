"""Kill switch: one halt flag in Redis that every trading process obeys. Set by hand (`mp halt`,
the dashboard, Telegram) or by a breaker in the trader. Resuming is always a manual act: an
automatic halt never clears itself, because whatever tripped it needs a person to look."""

from __future__ import annotations

from datetime import datetime

from mp_core.store.kv import KV
from mp_core.trading.models import HaltState

KEY = "mp:trading:halt"


class KillSwitch:
    def __init__(self, kv: KV) -> None:
        self.kv = kv

    async def state(self) -> HaltState:
        raw = await self.kv.get(KEY)
        return HaltState.model_validate_json(raw) if raw else HaltState(halted=False)

    async def halt(self, reason: str, by: str, now: datetime, *, flatten: bool = False) -> HaltState:
        cur = await self.state()
        if cur.halted:  # keep the first reason: it is the one that explains what happened
            if flatten and not cur.flatten:  # ...but a later request to flatten is honoured
                cur = cur.model_copy(update={"flatten": True})
                await self.kv.set(KEY, cur.model_dump_json())
            return cur
        st = HaltState(halted=True, reason=reason, by=by, since=now, flatten=flatten)
        await self.kv.set(KEY, st.model_dump_json())
        return st

    async def resume(self, by: str) -> HaltState:
        if not by.startswith("manual:"):
            raise ValueError("only a person can resume trading (by='manual:<who>')")
        st = HaltState(halted=False)
        await self.kv.set(KEY, st.model_dump_json())
        return st
