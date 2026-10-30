"""The memory log: an append-only markdown record of each decision and, once settled, its outcome.

An intraday decision carries its paper plan as one JSON line under a
``HORIZON: intraday`` line, between the tag and the decision, and its outcome
joins them once the session has been scored; the tag grammar is the same. A
swing decision carries the benchmark it is measured against on a
``BENCHMARK:`` line there.
"""

import contextlib
import json
import re
from datetime import time
from pathlib import Path

from tradingagents.agents.rating import parse_rating
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.symbols import is_us_listing, normalize_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets.exchanges import exchange_for_symbol


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections."""

    # An HTML comment on a line of its own; model text never keeps such a line (_text_block).
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call.
    # A section label is a whole line, which model text never keeps either.
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:(?:\n|\Z)|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"(?:^|\n)REFLECTION:\n(.*)", re.DOTALL)
    # An intraday entry's labelled lines, before its DECISION section.
    INTRADAY_TAG = "HORIZON: intraday"
    _PLAN_LABEL = "INTRADAY PLAN: "
    _OUTCOME_LABEL = "INTRADAY OUTCOME: "
    # A swing entry's labelled line there: the benchmark it is measured against.
    _BENCHMARK_LABEL = "BENCHMARK: "

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")

    # --- Write: a run records its decision ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
        rating: str | None = None,
        intraday_plan: dict | None = None,
        benchmark: str | None = None,
    ) -> bool | None:
        """Append pending entry at end of propagate(). No LLM call.

        ``rating`` is the decision's own rating when the caller has it; without
        one it is read from the decision text. ``intraday_plan`` (an intraday
        run's plan, ``{}`` when it gave none) tags the entry intraday, so it is
        settled on its session's bars rather than over the holding period.
        ``benchmark`` is the index a swing decision is measured against, by
        whichever run settles it. Returns whether the entry was added: False
        when the log already holds one for this ticker, date and horizon; None
        when there is no log.
        """
        if not self._log_path:
            return None
        intraday = intraday_plan is not None
        with locked(self._log_path):
            # Idempotency guard: an entry for this ticker, date and horizon blocks
            # another, pending or settled: a re-run after the outcome landed would
            # otherwise count the same decision twice in past context and in every
            # aggregate over the log. A swing rating and an intraday plan for the
            # same day are two decisions, each settled its own way.
            if self._log_path.exists():
                for block in self._log_path.read_text(encoding="utf-8").split(self._SEPARATOR):
                    lines = block.strip().splitlines()
                    if (lines and lines[0].startswith(f"[{trade_date} | {ticker} |") and lines[0].endswith("]")
                            and self._is_intraday(lines) == intraday):
                        return False
            rating = rating or parse_rating(final_trade_decision)
            tag = f"[{trade_date} | {ticker} | {rating} | pending]"
            head = ""
            if intraday:
                head = f"{self.INTRADAY_TAG}\n{self._PLAN_LABEL}{_json_line(intraday_plan)}\n\n"
            elif benchmark:
                head = f"{self._BENCHMARK_LABEL}{benchmark}\n\n"
            entry = f"{tag}\n\n{head}DECISION:\n{_text_block(final_trade_decision)}{self._SEPARATOR}"
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry)
            return True

    def settling(self, wait: bool = True):
        """Hold the log for one settlement pass at a time, across threads and processes.

        A pass reads the pending entries, asks the model for a reflection on
        each and writes it; two passes at once would pay for the same
        reflections. A separate lock from the one each write takes. Yields
        whether it is held; with ``wait=False`` another pass is not waited for.
        """
        if not self._log_path:
            return contextlib.nullcontext(True)
        return locked(f"{self._log_path}.settle", wait=wait)

    # --- Read ---

    def load_entries(self) -> list[dict]:
        """Parse all entries from log. Returns list of dicts."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> list[dict]:
        """Return entries with outcome:pending, for settlement."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3, as_of: str | None = None
    ) -> str:
        """Return formatted past context string for agent prompt injection.

        When ``as_of`` (yyyy-mm-dd) is given, only lessons whose outcome was
        already known by that date are included — an entry is kept only if it
        stores a resolution date (``resolved:...``) that is on or before
        ``as_of``. This keeps a historical/backtest run from learning from
        outcomes that had not happened yet (#1251). ``as_of=None`` disables the
        filter, so live runs and pre-migration entries are unaffected. An
        outcome is known once its listing and its benchmark have both closed
        on that date, so one whose listing or benchmark is not known to close
        with ``ticker``'s (``_market_clock``: NSE with BSE, US listings
        together, one exchange's listings and its index together) counts only
        from the next day: its market may close after the run's, as NYSE does
        at 01:30 IST and a coin's UTC day does after NYSE. That holds back an
        outcome measured against another market's benchmark: an index, or a
        listing on an exchange ``benchmark_map`` does not name, against SPY by
        default, or an NSE listing with ``--benchmark SPY``. A swing outcome
        recorded without its benchmark was settled by a release that did not
        record it, against the one ``_measured_against`` names; an intraday
        outcome has none, and is judged by its listing alone.
        """
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if as_of is not None:
            clock = _market_clock(ticker)
            entries = [e for e in entries if e.get("resolved") and (
                e["resolved"] < as_of or (e["resolved"] == as_of and _market_clock(e["ticker"]) == clock
                                          and _market_clock(_measured_against(e)) == clock))]
        if not entries:
            return ""

        same, cross = [], []
        # Most recent by decision date, then by the later write: a backfill writes
        # older decisions last.
        newest = sorted(range(len(entries)), key=lambda i: (entries[i]["date"], i), reverse=True)
        for e in (entries[i] for i in newest):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append(e)

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(self._format_reflection_only(e) for e in cross)
        return "\n\n".join(parts)

    # --- Settle: record a decision's outcome and reflection ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
        resolution_date: str | None = None,
        benchmark: str | None = None,
    ) -> bool:
        """Replace pending tag and append REFLECTION section using atomic write.

        Returns whether an entry was updated: False when the decision was no
        longer pending (another pass settled it first).

        Finds the first pending entry matching (trade_date, ticker), updates
        its tag with return figures (and the ``resolution_date`` the outcome
        became known), and appends a REFLECTION section.  ``benchmark``, the
        one the alpha is measured against, is written into an entry recorded
        without one. Uses a temp-file + os.replace() so a crash mid-write
        never corrupts the log.
        """
        if not self._log_path or not self._log_path.exists():
            return False
        with locked(self._log_path):
            text = self._log_path.read_text(encoding="utf-8")
            blocks = text.split(self._SEPARATOR)

            pending_prefix = f"[{trade_date} | {ticker} |"
            raw_pct = f"{raw_return:+.1%}"
            alpha_pct = f"{alpha_return:+.1%}"

            updated = False
            new_blocks = []
            for block in blocks:
                stripped = block.strip()
                if not stripped:
                    new_blocks.append(block)
                    continue

                lines = stripped.splitlines()
                tag_line = lines[0].strip()

                if (
                    not updated
                    and tag_line.startswith(pending_prefix)
                    and tag_line.endswith("| pending]")
                    and not self._is_intraday(lines)
                ):
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    new_tag = self._resolved_tag(
                        trade_date, ticker, rating, raw_pct, alpha_pct, holding_days, resolution_date
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{self._settled_body(rest, benchmark)}\n\nREFLECTION:\n{_text_block(reflection)}"
                    )
                    updated = True
                else:
                    new_blocks.append(block)

            if not updated:
                return False

            new_blocks = self._apply_rotation(new_blocks)
            new_text = self._SEPARATOR.join(new_blocks)
            tmp_path = self._log_path.with_suffix(".tmp")
            tmp_path.write_text(new_text, encoding="utf-8")
            tmp_path.replace(self._log_path)
            return True

    def update_with_intraday_outcome(
        self,
        ticker: str,
        trade_date: str,
        outcome: dict,
        reflection: str,
        resolution_date: str | None = None,
    ) -> bool:
        """Settle an intraday entry: its tag names the plan's net return, and its outcome and reflection join it.

        The tag reads ``[date | ticker | rating | net | n/a | intraday |
        resolved:session]``: the net return as a percentage of the position's
        value at entry ("n/a" with no trade), no alpha (there is no benchmark
        for one session), and the session the outcome became known. Returns
        whether an entry was updated, as ``update_with_outcome`` does.
        """
        if not self._log_path or not self._log_path.exists():
            return False
        net = outcome.get("net_return_pct")
        raw_pct = f"{net / 100:+.1%}" if isinstance(net, (int, float)) else "n/a"
        with locked(self._log_path):
            text = self._log_path.read_text(encoding="utf-8")
            blocks = text.split(self._SEPARATOR)
            pending_prefix = f"[{trade_date} | {ticker} |"
            for index, block in enumerate(blocks):
                lines = block.strip().splitlines()
                if not lines:
                    continue
                tag_line = lines[0].strip()
                if not (tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]")
                        and self._is_intraday(lines)):
                    continue
                rating = [f.strip() for f in tag_line[1:-1].split("|")][2]
                tag = f"[{trade_date} | {ticker} | {rating} | {raw_pct} | n/a | intraday"
                tag += f" | resolved:{resolution_date}]" if resolution_date else "]"
                head, sep, rest = "\n".join(lines[1:]).lstrip().partition("\n\nDECISION:\n")
                # Escaped again, as _settled_body does a swing decision's.
                body = f"{head}\n{self._OUTCOME_LABEL}{_json_line(outcome)}{sep}{_text_block(rest)}"
                blocks[index] = f"{tag}\n\n{body}\n\nREFLECTION:\n{_text_block(reflection)}"
                break
            else:
                return False
            new_blocks = self._apply_rotation(blocks)
            tmp_path = self._log_path.with_suffix(".tmp")
            tmp_path.write_text(self._SEPARATOR.join(new_blocks), encoding="utf-8")
            tmp_path.replace(self._log_path)
            return True

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return
        with locked(self._log_path):
            text = self._log_path.read_text(encoding="utf-8")
            blocks = text.split(self._SEPARATOR)

            update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

            new_blocks = []
            for block in blocks:
                stripped = block.strip()
                if not stripped:
                    new_blocks.append(block)
                    continue

                lines = stripped.splitlines()
                tag_line = lines[0].strip()

                matched = False
                for (trade_date, ticker), upd in list(update_map.items()):
                    pending_prefix = f"[{trade_date} | {ticker} |"
                    if (tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]")
                            and not self._is_intraday(lines)):
                        fields = [f.strip() for f in tag_line[1:-1].split("|")]
                        rating = fields[2]
                        raw_pct = f"{upd['raw_return']:+.1%}"
                        alpha_pct = f"{upd['alpha_return']:+.1%}"
                        new_tag = self._resolved_tag(
                            trade_date, ticker, rating, raw_pct, alpha_pct,
                            upd["holding_days"], upd.get("resolution_date"),
                        )
                        rest = "\n".join(lines[1:])
                        new_blocks.append(
                            f"{new_tag}\n\n{self._settled_body(rest)}\n\nREFLECTION:\n{_text_block(upd['reflection'])}"
                        )
                        del update_map[(trade_date, ticker)]
                        matched = True
                        break

                if not matched:
                    new_blocks.append(block)

            new_blocks = self._apply_rotation(new_blocks)
            new_text = self._SEPARATOR.join(new_blocks)
            tmp_path = self._log_path.with_suffix(".tmp")
            tmp_path.write_text(new_text, encoding="utf-8")
            tmp_path.replace(self._log_path)

    # --- Helpers ---

    def _settled_body(self, rest: str, benchmark: str | None = None) -> str:
        """A pending swing entry's text after its tag, as its settled entry keeps it: ``benchmark`` labelled
        before the decision when the entry names none, and the decision escaped again (``_text_block`` leaves
        escaped text as it is). One recorded before decisions were escaped may end in a separator or label
        line with a space after it, which would end the entry or open a section once that space is stripped."""
        head, sep, decision = rest.lstrip().partition("DECISION:\n")
        if not sep:
            return rest.lstrip()
        if benchmark and _labelled_line(head, self._BENCHMARK_LABEL) is None:
            head += f"{self._BENCHMARK_LABEL}{benchmark}\n\n"
        return f"{head}{sep}{_text_block(decision)}"

    def _is_intraday(self, lines: list[str]) -> bool:
        """Whether an entry's lines (its tag first) carry the intraday tag before its decision."""
        for line in lines[1:]:
            text = line.strip()
            if text == "DECISION:":
                return False
            if text == self.INTRADAY_TAG:
                return True
        return False

    @staticmethod
    def _resolved_tag(
        trade_date, ticker, rating, raw_pct, alpha_pct, holding_days, resolution_date
    ) -> str:
        """Build a resolved entry tag, recording the outcome's known-by date.

        ``resolution_date`` (the date of the last price bar used for the return)
        is the point-in-time cutoff a later run filters on (#1251). Omitted when
        unavailable, keeping the legacy 6-field tag.
        """
        tag = f"[{trade_date} | {ticker} | {rating} | {raw_pct} | {alpha_pct} | {holding_days}d"
        if resolution_date:
            tag += f" | resolved:{resolution_date}"
        return tag + "]"

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        """Drop the oldest-dated resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (block, is_resolved, decision date) from its tag line.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False, ""))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
            )
            decisions.append((block, is_resolved, tag_line[1:].split("|")[0].strip()))

        resolved_count = sum(1 for _, r, _ in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        # The oldest by decision date, then by position: a backfill writes older
        # decisions last.
        resolved = [i for i, (_, is_resolved, _) in enumerate(decisions) if is_resolved]
        dated = sorted(resolved, key=lambda i: (decisions[i][2], i))
        dropped = set(dated[:resolved_count - self._max_entries])
        return [block for i, (block, _, _) in enumerate(decisions) if i not in dropped]

    def _parse_entry(self, raw: str) -> dict | None:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        # Optional trailing "resolved:YYYY-MM-DD" field records when the outcome
        # became known, for point-in-time filtering (#1251).
        resolved = None
        for f in fields[6:]:
            if f.startswith("resolved:"):
                resolved = f[len("resolved:"):].strip()
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
            "resolved": resolved,
        }
        body = "\n".join(lines[1:]).strip()
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        # The labelled lines before the decision; the decision's own text is never read for them.
        head = body.split("DECISION:\n", 1)[0] if "DECISION:\n" in body else ""
        benchmark = _labelled_line(head, self._BENCHMARK_LABEL)
        if benchmark:
            entry["benchmark"] = benchmark
        if self.INTRADAY_TAG in head.splitlines():
            entry["horizon"] = "intraday"
            entry["intraday_plan"] = _labelled_json(head, self._PLAN_LABEL)
            entry["intraday_outcome"] = _labelled_json(head, self._OUTCOME_LABEL)
        return entry

    def _format_full(self, e: dict) -> str:
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        if e.get("benchmark"):
            alpha = f"{alpha} vs {e['benchmark']}"
        holding = e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag]
        if e.get("horizon") == "intraday":
            parts.append(f"INTRADAY OUTCOME: {describe_outcome(e.get('intraday_plan'), e.get('intraday_outcome'))}")
        parts.append(f"DECISION:\n{e['decision']}")
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str:
        horizon = " | intraday" if e.get("horizon") == "intraday" else ""
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}{horizon}]"
        if e["reflection"]:
            return f"{tag}\n{e['reflection']}"
        text = e["decision"][:300]
        suffix = "..." if len(e["decision"]) > 300 else ""
        return f"{tag}\n{text}{suffix}"


# Line breaks str.splitlines() reads that JSON leaves unescaped outside ASCII.
_UNESCAPED_BREAKS = {0x85: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"}


def _json_line(value: dict) -> str:
    """``value`` as one line of JSON. Every line break in it is escaped, so model text
    it carries can never end the entry, start another one or split the line."""
    return json.dumps(value, ensure_ascii=False, default=str).translate(_UNESCAPED_BREAKS)


# The line breaks str.splitlines() reads besides "\n", which reading and rewriting the log would turn into "\n".
_OTHER_BREAKS = re.compile(r"\r\n?|[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
# Lines the log reads as an entry's end or a section label, whatever spaces surround them: settling rebuilds
# an entry from its stripped text, and _is_intraday reads stripped lines.
_STRUCTURE_LINE = re.compile(
    rf"^([^\S\n]*)({re.escape(TradingMemoryLog._SEPARATOR.strip())}|DECISION:|REFLECTION:)(?=[^\S\n]*$)",
    re.MULTILINE)


def _text_block(text) -> str:
    """Model text (a decision, a reflection) as the log stores it: its line breaks as "\\n", no whitespace at
    its end (settling would strip it), and a line that would end the entry or open a section, spaces aside,
    behind a backslash, so it can only be read as text."""
    return _STRUCTURE_LINE.sub(r"\1\\\2", _OTHER_BREAKS.sub("\n", str(text)).rstrip())


# A pair quoted in a currency (BNB-USD, BTC-INR), which is_us_listing takes for a US share class (BRK-B,
# BAC-PL: at most two letters after the dash) when it does not know the coin.
_QUOTED_PAIR = re.compile(r"-[A-Z]{3,}$")


def _market_clock(ticker: str) -> tuple:
    """When ``ticker``'s close is final, as a key two tickers share only when they are known to close
    together: an exchange's known here (NSE and BSE close together), New York's 16:00 for a plain US listing,
    else its listing suffix, which names one exchange (``.T``, ``.L``), as it does for the exchange's index
    in the default ``benchmark_map`` (^N225 closes with ``.T`` listings). An index, future, currency or coin
    pair outside those keeps a key of its own."""
    symbol = normalize_symbol(ticker) or ""
    exchange = exchange_for_symbol(symbol)
    if exchange:
        return (exchange.timezone, exchange.close_final)
    if is_us_listing(symbol) and not _QUOTED_PAIR.search(symbol):
        return ("America/New_York", time(16, 0))
    if "." in symbol:
        return ("listing", symbol.rsplit(".", 1)[1])
    # The first suffix naming an index is its exchange's (TAIEX is .TW's, though .TWO also takes it).
    suffix = next((suffix for suffix, index in (DEFAULT_CONFIG.get("benchmark_map") or {}).items()
                   if suffix and normalize_symbol(index) == symbol), None)
    if suffix:
        return ("listing", suffix.lstrip(".").upper())
    return ("instrument", symbol)


def _measured_against(entry: dict) -> str:
    """What a settled entry's outcome is measured against: the benchmark it records, else its own listing for
    an intraday plan (scored on its session's bars), else what the releases that recorded none (27e6744, and
    v0.7.1 before it) settled it against with no benchmark_ticker set: the default ``benchmark_map`` index of
    its suffix, else SPY, for an index too (^NSEI, ^N225). settle_pending records the benchmark of every swing
    decision it settles."""
    if entry.get("benchmark"):
        return entry["benchmark"]
    if entry.get("horizon") == "intraday":
        return entry["ticker"]
    benchmark_map = DEFAULT_CONFIG.get("benchmark_map") or {}
    symbol = normalize_symbol(entry["ticker"]) or ""
    return next((index for suffix, index in benchmark_map.items() if suffix and symbol.endswith(suffix.upper())),
                benchmark_map.get("", "SPY"))


def _labelled_line(head: str, label: str) -> str | None:
    """The rest of ``head``'s line starting with ``label``; None when there is none."""
    return next((line[len(label):].strip() for line in head.splitlines() if line.startswith(label)), None)


def _labelled_json(head: str, label: str) -> dict | None:
    """The JSON object on ``head``'s line starting with ``label``; None when there is none or it does not parse."""
    for line in head.splitlines():
        if line.startswith(label):
            try:
                value = json.loads(line[len(label):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _number(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "n/a"


def describe_outcome(plan: dict | None, outcome: dict | None) -> str:
    """One line on what an intraday plan did, for the lessons later runs read."""
    plan, outcome = plan or {}, outcome or {}
    session = plan.get("session") or "its session"
    reason = outcome.get("exit_reason")
    if outcome.get("scored") is False or not reason:
        return f"{session}: not scored ({outcome.get('note') or 'no outcome recorded'})"
    if not outcome.get("triggered"):
        note = outcome.get("note") or reason.replace("_", " ")
        return f"{session}: no trade ({note})"
    line = (f"{session}: {plan.get('direction')} filled at {_number(outcome.get('entry_price'))}, closed at "
            f"{_number(outcome.get('exit_price'))} ({reason.replace('_', ' ')}); net P&L "
            f"{_number(outcome.get('net_pnl'))}")
    if isinstance(outcome.get("net_return_pct"), (int, float)):
        line += f" ({outcome['net_return_pct']:+.2f}%)"
    if isinstance(outcome.get("r_multiple"), (int, float)):
        line += f", {outcome['r_multiple']:+.2f}R"
    if not outcome.get("shares"):
        line += ", per share"
    return line
