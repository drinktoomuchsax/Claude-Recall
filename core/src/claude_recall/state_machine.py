"""Priority-based state machine with TTL degradation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from claude_recall.config import StatesConfig
from claude_recall.models import RecallState


STATE_NAME_MAP: dict[str, RecallState] = {s.name.lower(): s for s in RecallState}


def state_from_name(name: str) -> RecallState:
    return STATE_NAME_MAP[name.lower()]


class StateMachine:
    def __init__(self, config: StatesConfig):
        self._config = config
        self._current: RecallState = RecallState.OFF
        self._set_at: datetime = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    @property
    def current(self) -> RecallState:
        return self._current

    @property
    def effective_state(self) -> RecallState:
        """Current state, accounting for TTL expiry."""
        if self._is_expired():
            return self._degrade_target()
        return self._current

    @property
    def state_since(self) -> datetime:
        return self._set_at

    async def transition(self, new_state: RecallState) -> tuple[RecallState, bool]:
        """
        Attempt state transition. Returns (resulting_state, did_change).

        Rules:
        - OFF is always accepted (forced by SessionEnd)
        - Higher-or-equal priority: accepted immediately
        - Lower priority: accepted only if current state TTL expired
        """
        async with self._lock:
            old = self.effective_state

            # SessionEnd forces OFF
            if new_state == RecallState.OFF:
                return self._apply(new_state, old)

            # Higher or equal priority: accept
            if new_state.value >= old.value:
                return self._apply(new_state, old)

            # Lower priority: accept only if expired
            if self._is_expired():
                return self._apply(new_state, old)

            return old, False

    def _apply(self, new_state: RecallState, old: RecallState) -> tuple[RecallState, bool]:
        self._current = new_state
        self._set_at = datetime.now(timezone.utc)
        return new_state, new_state != old

    def _is_expired(self) -> bool:
        ttl_config = self._get_ttl_config()
        if ttl_config is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._set_at).total_seconds()
        return elapsed >= ttl_config.ttl_sec

    def _degrade_target(self) -> RecallState:
        ttl_config = self._get_ttl_config()
        if ttl_config is None:
            return self._current
        return state_from_name(ttl_config.degrade_to)

    def _get_ttl_config(self):
        name = self._current.name.lower()
        return getattr(self._config, name, None)
