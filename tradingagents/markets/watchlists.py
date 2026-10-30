"""Named lists of symbols to scan or backtest, from the config and a JSON file the user keeps.

Nothing here ships index constituents: they change at every index rebalance, so
a list that names them is the user's to keep (or to download from the index
provider), dated by them. The file maps a name to its symbols:

    {"banks": ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS"], "it": ["TCS", "INFY", "HCLTECH"]}

Bare symbols are listed on the run's default exchange, as a ticker is.
"""

import json
from collections.abc import Callable
from pathlib import Path


def load_watchlists(config: dict) -> dict[str, list[str]]:
    """Every watchlist, by lower-cased name: the config's ``watchlists``, then the file's."""
    lists = {str(name).lower(): list(symbols) for name, symbols in (config.get("watchlists") or {}).items()}
    path = config.get("watchlists_file")
    if path and Path(path).expanduser().is_file():
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"watchlists file {path} is not usable: {exc}") from exc
        if not isinstance(data, dict) or not all(
                isinstance(v, list) and all(isinstance(s, str) for s in v) for v in data.values()):
            raise ValueError(f"watchlists file {path} must map each name to a list of symbols")
        lists.update({str(name).lower(): symbols for name, symbols in data.items()})
    return lists


def watchlist_symbols(spec: str, config: dict, resolve: Callable[[str], str]) -> list[str]:
    """A named watchlist's symbols, else ``spec`` read as comma-separated symbols; each resolved, once."""
    if not spec or not spec.strip():
        raise ValueError("name a watchlist or give comma-separated symbols")
    raw = load_watchlists(config).get(spec.strip().lower())
    if raw is None:
        raw = [s for s in spec.split(",") if s.strip()]
    seen, symbols = set(), []
    for symbol in (resolve(s) for s in raw):
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if not symbols:
        raise ValueError(f"watchlist {spec!r} has no symbols")
    return symbols
