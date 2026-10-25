"""Catalogue mining + order-2 Markov shortlist (design §1 "candidate generator").

Catalogue: tokens recurring >= MIN_COUNT times in the fitted window are primitives
(parameterised tokens such as `activate:<App>` or `url:<host>/<slug>` are kept as-is).
Everything else maps to OTHER in histories and outcomes; OTHER is never proposed.
afk edges are not primitives (nobody "chooses" to go afk from the HUD).

Model: outcome = the moment's label (a primitive, `nothing`, or OTHER), conditioned on
a chain of contexts from general to specific:
  prior -> (h1) -> (h1, hour_bucket) -> (h2, h1, hour_bucket) -> (h2, h1, hour_bucket, space)
The last level is used only when the space is known. Probabilities are interpolated
up the chain, Witten-Bell style: p_l = lam * ML_l + (1 - lam) * p_{l-1},
lam = n / (n + ALPHA * distinct), so an unseen context backs off to the order-1 level and
then to the prior. Pure counting: fit is deterministic and ties sort by token name.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .moments import NOTHING
from .paths import data_dir

MIN_COUNT = 5
ALPHA = 1.0
OTHER = "<other>"
START = "<s>"
SEP = "\t"
LEVELS = ["prior", "o1", "o1h", "o2h", "o2hs"]
FORMAT = "oi-markov/1"


def mine_catalogue(tokens: list[str], min_count: int = MIN_COUNT) -> dict[str, int]:
    c = Counter(t for t in tokens if not t.startswith(("afk:", "wish:", "hud:", "vocal:")))  # feedback is never a primitive
    return {t: n for t, n in sorted(c.items()) if n >= min_count}


def _keys(h2: str, h1: str, hb, space) -> list[tuple[str, str]]:
    ks = [("prior", ""), ("o1", h1), ("o1h", f"{h1}{SEP}{hb}"), ("o2h", f"{h2}{SEP}{h1}{SEP}{hb}")]
    if space is not None:
        ks.append(("o2hs", f"{h2}{SEP}{h1}{SEP}{hb}{SEP}{space}"))
    return ks


class Markov:
    def __init__(self, catalogue: dict[str, int], counts: dict, *, min_count=MIN_COUNT,
                 alpha=ALPHA, meta: dict | None = None):
        self.catalogue = catalogue
        self.counts = counts  # level -> key -> {outcome: n}
        self.min_count = min_count
        self.alpha = alpha
        self.meta = meta or {}

    # ---- features -------------------------------------------------------
    def _map(self, tok: str | None) -> str:
        if tok is None:
            return START
        return tok if tok in self.catalogue else OTHER

    def _outcome(self, label: str) -> str:
        return label if (label == NOTHING or label in self.catalogue) else OTHER

    def features(self, moment: dict) -> tuple[str, str, int, object]:
        hist = moment.get("history") or []
        h1 = self._map(hist[0]["token"] if hist else moment["context"].get("token"))
        h2 = self._map(hist[1]["token"] if len(hist) > 1 else None)
        ctx = moment["context"]
        return h2, h1, ctx.get("hour_bucket"), ctx.get("space")

    # ---- fit --------------------------------------------------------------
    @classmethod
    def fit(cls, moments: list[dict], *, min_count=MIN_COUNT, alpha=ALPHA,
            tokens: list[str] | None = None, meta: dict | None = None) -> "Markov":
        """tokens: the window's token stream for the catalogue (default: moment tokens)."""
        if tokens is None:
            tokens = [m["context"]["token"] for m in moments]
        m = cls(mine_catalogue(tokens, min_count), {}, min_count=min_count, alpha=alpha, meta=meta)
        counts: dict = {lv: defaultdict(Counter) for lv in LEVELS}
        for mo in moments:
            y = m._outcome(mo["label"])
            for lv, k in _keys(*m.features(mo)):
                counts[lv][k][y] += 1
        m.counts = {lv: {k: dict(sorted(c.items())) for k, c in sorted(d.items())}
                    for lv, d in counts.items()}
        return m

    # ---- predict ------------------------------------------------------------
    def distribution(self, moment: dict) -> tuple[dict[str, float], str]:
        """Interpolated P(outcome | context) and the most specific level with data."""
        probs: dict[str, float] = {}
        deepest = "none"
        for lv, k in _keys(*self.features(moment)):
            c = self.counts.get(lv, {}).get(k)
            if not c:
                continue  # unseen at this level: keep the lower-order estimate
            n = sum(c.values())
            if not probs:
                probs = {y: v / n for y, v in c.items()}
            else:
                lam = n / (n + self.alpha * len(c))
                ys = set(probs) | set(c)
                probs = {y: lam * c.get(y, 0) / n + (1 - lam) * probs.get(y, 0.0) for y in ys}
            deepest = lv
        return probs, deepest

    def top_k(self, moment: dict, k: int = 10, *, include_nothing=True) -> list[tuple[str, float]]:
        probs, _ = self.distribution(moment)
        items = [(y, p) for y, p in probs.items()
                 if y != OTHER and (include_nothing or y != NOTHING) and p > 0]
        items.sort(key=lambda yp: (-round(yp[1], 12), yp[0]))
        return [(y, round(p, 6)) for y, p in items[:k]]

    # ---- persistence ----------------------------------------------------------
    def to_json(self) -> dict:
        return {"format": FORMAT, "min_count": self.min_count, "alpha": self.alpha,
                "meta": self.meta, "catalogue": self.catalogue, "counts": self.counts}

    @classmethod
    def from_json(cls, d: dict) -> "Markov":
        if d.get("format") != FORMAT:
            raise ValueError(f"not an {FORMAT} model")
        return cls(d["catalogue"], d["counts"], min_count=d["min_count"], alpha=d["alpha"],
                   meta=d.get("meta"))

    def save(self, path: Path | None = None, day: date | None = None) -> Path:
        if path is None:
            path = data_dir() / "models" / f"markov-{(day or date.today()).isoformat()}.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, indent=1))
        return path

    @classmethod
    def load(cls, path: Path) -> "Markov":
        return cls.from_json(json.loads(Path(path).read_text()))


def latest_model_path() -> Path | None:
    hits = sorted((data_dir() / "models").glob("markov-*.json"))
    return hits[-1] if hits else None
