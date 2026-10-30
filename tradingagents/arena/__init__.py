"""Paper trades on AlphArena (alpharena.io), a public arena where trading agents compete with simulated money.

Off by default. When it is turned on, a run's swing decision on a US stock or ETF, dated
today in New York, can be sent as a paper trade once the person confirms it:

- ``alpharena``: the REST client. Its base URL is fixed in code, so no setting can send
  the key elsewhere; the key is read from ``ALPHARENA_API_KEY`` at each call, checked
  against ``arena_mode``, and never logged.
- ``mapping``: which runs may be sent (``refusal``), what a rating becomes
  (``plan_trades``, or ``propose_plan`` for a finished run; ``close_plan`` for a close the
  person asks for), and ``send_plan``, which sends a confirmed plan and records each
  request in the ledger.
- ``ledger``: a local JSONL record of every open and close sent, and AlphArena's answer.
  Reads (open trades, status, leaderboard) are not recorded.

Indian listings are never sent: SEBI has cautioned against public paper-trading platforms
built on Indian listed-stock prices, so NSE/BSE decisions stay in the local paper log.
Nothing here places a real order, and no arena answer reaches an LLM prompt.
"""

from tradingagents.arena.alpharena import BASE_URL, KEY_ENV, AlphArenaClient, ArenaError, plain_text
from tradingagents.arena.ledger import ArenaLedger, ledger_path
from tradingagents.arena.mapping import (
    CONFIDENCE,
    SEBI_REFUSAL,
    ArenaPlan,
    close_plan,
    decision_fields,
    idempotency_key,
    latest_close,
    new_york_today,
    plan_trades,
    propose_plan,
    refusal,
    report_note,
    send_plan,
    ticker_refusal,
)

__all__ = [
    "BASE_URL",
    "CONFIDENCE",
    "KEY_ENV",
    "SEBI_REFUSAL",
    "AlphArenaClient",
    "ArenaError",
    "ArenaLedger",
    "ArenaPlan",
    "close_plan",
    "decision_fields",
    "idempotency_key",
    "latest_close",
    "ledger_path",
    "new_york_today",
    "plain_text",
    "plan_trades",
    "propose_plan",
    "refusal",
    "report_note",
    "send_plan",
    "ticker_refusal",
]
