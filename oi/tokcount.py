"""Token counting for the Laya state window.

Exact path: Laya's own tokenizer (ModernBERT, `tokenizer/tokenizer.json` of the
`convaiinnovations/laya` checkpoint, the same file laya-coreml's registry loads via
AutoTokenizer) through the `tokenizers` package (`pip install -e .[laya]`).
Lookup order for the file: $OI_LAYA_TOKENIZER, then the Hugging Face cache
(~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/tokenizer/tokenizer.json).

Fallback (no `tokenizers` or no file): ceil(1.7 x number of word/punctuation pieces),
regex `\\w+|[^\\w\\s]`. Measured on the synthetic states: Laya/pieces ratio 1.06-1.51
(symbols such as · and ⌘ split into several byte tokens), hence 1.7, which over-counts
on our state texts (checked against the real tokenizer in tests/test_tokcount_cli.py
when available): a state that fits under the fallback fits under Laya.

Counts exclude special tokens ([CLS]/[SEP]); keep a few tokens of slack under a bucket.
"""
from __future__ import annotations

import functools
import glob
import math
import os
import re

_PIECES = re.compile(r"\w+|[^\w\s]")
FALLBACK_NAME = "fallback:regex-pieces-x1.7"


def _find_tokenizer_json() -> str | None:
    env = os.environ.get("OI_LAYA_TOKENIZER")
    if env:
        return env if os.path.isfile(env) else None
    pat = os.path.expanduser(
        "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/tokenizer/tokenizer.json"
    )
    hits = sorted(glob.glob(pat))
    return hits[-1] if hits else None


@functools.lru_cache(maxsize=1)
def _load():
    if os.environ.get("OI_TOKENIZER") == "fallback":
        return None, FALLBACK_NAME
    path = _find_tokenizer_json()
    if not path:
        return None, FALLBACK_NAME
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None, FALLBACK_NAME
    snap = path.split("/snapshots/")[1].split("/")[0][:7] if "/snapshots/" in path else "local"
    return Tokenizer.from_file(path), f"laya:convaiinnovations/laya@{snap}"


def tokenizer_name() -> str:
    return _load()[1]


def fallback_count(text: str) -> int:
    return math.ceil(1.7 * len(_PIECES.findall(text)))


def count_tokens(text: str) -> int:
    tok, _ = _load()
    if tok is None:
        return fallback_count(text)
    return len(tok.encode(text, add_special_tokens=False).ids)


# Report the active tokenizer as a module-level name (resolved lazily on first access).
def __getattr__(name):
    if name == "TOKENIZER":
        return tokenizer_name()
    raise AttributeError(name)
