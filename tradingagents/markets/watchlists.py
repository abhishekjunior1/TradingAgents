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


def watchlist_symbols(spec: str, config: dict, resolve: Callable[[str], str], names_only: bool = False) -> list[str]:
    """A named watchlist's symbols, else ``spec`` read as comma-separated symbols; each resolved, once.

    With ``names_only`` a name the lists do not have is refused, naming the lists there
    are and the file read, rather than read as a symbol.
    """
    if not spec or not spec.strip():
        raise ValueError("name a watchlist or give comma-separated symbols")
    lists = load_watchlists(config)
    raw = lists.get(spec.strip().lower())
    if raw is None and names_only:
        raise ValueError(_unknown_name(spec.strip(), lists, config.get("watchlists_file")))
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


def _unknown_name(name: str, lists: dict[str, list[str]], path: str | None) -> str:
    """Why ``name`` is no watchlist: the lists there are, and where they were looked for."""
    if not path:
        where = "the config's watchlists"
    elif Path(path).expanduser().is_file():
        where = f"{path} or the config's watchlists"
    else:
        where = f"the config's watchlists, and {path} does not exist"
    return f"no watchlist named {name!r} in {where}; known lists: {', '.join(sorted(lists)) or 'none'}"
