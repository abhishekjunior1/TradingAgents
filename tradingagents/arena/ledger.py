"""A local record of every open and close sent to AlphArena: what was sent and what came back.

One JSON object per line, at ``arena_ledger_path`` (default
``<results_dir>/arena/ledger.jsonl``): the time (UTC), mode, ticker, trade date, rating,
action, the decision's idempotency key, the payload, and AlphArena's response or the
error. Each line is appended whole while holding the file's lock, so runs in parallel
never interleave or drop each other's entries; a line cut short by a crash is skipped when
read. The ledger holds no key: fields that name one are redacted, and the current
``ALPHARENA_API_KEY`` is replaced wherever it appears.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.arena.alpharena import KEY_ENV
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.files import locked

# Fields that would carry a key: registration answers name them (registration is not
# implemented, so none should ever arrive).
_SECRET_FIELDS = frozenset({"api_key", "sandbox_api_key", "authorization"})


def ledger_path(config: dict | None = None) -> Path:
    """Where the ledger lives: ``arena_ledger_path``, else ``<results_dir>/arena/ledger.jsonl``."""
    cfg = get_config() if config is None else config
    configured = cfg.get("arena_ledger_path")
    if configured:
        return Path(configured).expanduser()
    return Path(cfg["results_dir"]).expanduser() / "arena" / "ledger.jsonl"


class ArenaLedger:
    """Append-only JSONL record of AlphArena requests and their answers."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else ledger_path()

    def __repr__(self) -> str:
        return f"ArenaLedger({str(self.path)!r})"

    def append(self, entry: dict) -> dict:
        """Write ``entry`` as one line, stamped with the time unless it has one; returns it as written."""
        record = _redact({"time": datetime.now(UTC).isoformat(timespec="seconds"), **entry})
        line = _scrub(json.dumps(record, ensure_ascii=False, default=str))
        data = (line + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path), open(self.path, "a+b") as handle:
            # A line a crash cut short stays on its own line, not fused with this one.
            if handle.seek(0, os.SEEK_END):
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    data = b"\n" + data
            handle.write(data)
        return json.loads(line)

    def entries(self) -> list[dict]:
        """Every entry, oldest first. A line that cannot be read is skipped."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        found = []
        # Decoded line by line, so a line cut inside a character is skipped like any
        # other; split on line ends only, as JSON escapes those within a value.
        for line in data.splitlines():
            try:
                entry = json.loads(line.decode("utf-8"))
            except ValueError:  # UnicodeDecodeError included
                continue
            if isinstance(entry, dict):
                found.append(entry)
        return found

    def already_submitted(self, idempotency_key: str, mode: str | None = None) -> bool:
        """Whether a trade was opened for this decision: an open with this key that AlphArena
        accepted. A failed attempt does not count, so it can be tried again; with ``mode``,
        only that mode's entries count (a sandbox trade does not stand for a live one)."""
        return any(
            entry.get("idempotency_key") == idempotency_key
            and str(entry.get("action", "")).startswith("open")
            and isinstance(entry.get("response"), dict) and not entry.get("error")
            and (mode is None or entry.get("mode") == mode)
            for entry in self.entries()
        )


def _redact(value):
    if isinstance(value, dict):
        return {k: "***" if str(k).lower() in _SECRET_FIELDS else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _scrub(line: str) -> str:
    """``line`` with the current key replaced, as written and as JSON escapes it."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        return line
    return line.replace(key, "***").replace(json.dumps(key)[1:-1], "***")
