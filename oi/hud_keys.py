"""W21: the HUD option NUMBER is its KEY, never its rank.

"commande je veux tout le temps que ce soit 4, option tout le temps 3, contrôle tout le temps 2, et fn tout le temps
1, parce que mes doigts ne vont pas bouger": fn = 1, ⌃ = 2, ⌥ = 3, ⌘ = 4 (oi-hud's `HotkeySet.defaults`; the
option's place and colour follow the key: fn left, ⌃ top, ⌥ right, ⌘ bottom). With fewer than 4 options the
missing keys' places stay empty and each option keeps its key's number.

Which option goes on which key is decided here: rank 1 (most likely) on the first key of `hud.fill_order`
(default ⌃ ⌥ ⌘ fn: the most likely on control), rank 2 on the next free one, … An option shown again in the same
context keeps its key when that key is free (stability over rank: the fingers learn where it is).
"""
from __future__ import annotations

from collections import OrderedDict

KEY_OF = {"fn": 1, "ctrl": 2, "option": 3, "cmd": 4}
ALIASES = {"fn": "fn", "globe": "fn", "ctrl": "ctrl", "control": "ctrl", "⌃": "ctrl", "option": "option",
           "opt": "option", "alt": "option", "⌥": "option", "cmd": "cmd", "command": "cmd", "⌘": "cmd"}
DEFAULT_FILL_ORDER = ("ctrl", "option", "cmd", "fn")
MAX_CONTEXTS = 64


def fill_order(raw) -> list[str]:
    """settings `hud.fill_order` -> four canonical modifier names; anything but a permutation of the four option
    modifiers gives the default (same rule as oi-settings' HUDSettings)."""
    if isinstance(raw, (list, tuple)) and all(isinstance(m, str) for m in raw):
        names = [ALIASES.get(m.strip().lower()) for m in raw]
        if len(names) == 4 and set(names) == set(KEY_OF):
            return names
    return list(DEFAULT_FILL_ORDER)


def fill_keys(order=None) -> list[int]:
    """The key numbers in fill order: [2, 3, 4, 1] by default."""
    return [KEY_OF[m] for m in fill_order(order if order is not None else DEFAULT_FILL_ORDER)]


def assign(ids: list[str], order=None, previous: dict[int, str] | None = None) -> list[int]:
    """One key per ranked option id (rank 1 first). An id that sat on a key last time in this context (`previous`
    = {key: id}) keeps it; the others take the free keys in fill order, by rank. At most 4 ids."""
    keys = fill_keys(order)
    if len(ids) > len(keys):
        raise ValueError(f"{len(ids)} options for {len(keys)} keys")
    was = {i: k for k, i in (previous or {}).items() if k in keys}
    out: list[int | None] = [None] * len(ids)
    used: set[int] = set()
    for n, i in enumerate(ids):                        # stability first (by rank when two claim one key)
        k = was.get(i)
        if k is not None and k not in used:
            out[n] = k
            used.add(k)
    free = [k for k in keys if k not in used]
    for n in range(len(ids)):
        if out[n] is None:
            out[n] = free.pop(0)
    return out  # type: ignore[return-value]


class KeyMemory:
    """Per context (e.g. form + front app): the id last shown on each key. Bounded (LRU of contexts)."""

    def __init__(self, max_contexts: int = MAX_CONTEXTS):
        self.max_contexts = max_contexts
        self._by_ctx: OrderedDict[str, dict[int, str]] = OrderedDict()

    def assign(self, ctx_key: str, ids: list[str], order=None) -> list[int]:
        prev = self._by_ctx.get(ctx_key, {})
        keys = assign(ids, order, prev)
        mem = {k: i for k, i in prev.items() if i not in ids}   # an id lives on one key: where it is now
        for k, i in zip(keys, ids):
            mem[k] = i
        self._by_ctx[ctx_key] = mem
        self._by_ctx.move_to_end(ctx_key)
        while len(self._by_ctx) > self.max_contexts:
            self._by_ctx.popitem(last=False)
        return keys
