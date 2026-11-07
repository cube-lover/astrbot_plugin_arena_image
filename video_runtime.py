"""Endpoint-scoped busy guard: no browser/profile creation and no model choices."""
import asyncio
from .arena_direct import cdp_browser_scope
_LOCKS = {}

def browser_lock(endpoint):
    scope = cdp_browser_scope(endpoint)
    # Plugin tasks share one event loop; tests/reloads may create another loop.
    return _LOCKS.setdefault((scope, asyncio.get_running_loop()), asyncio.Lock())
