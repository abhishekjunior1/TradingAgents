"""AlphArena wiring: the settings, the offer after a CLI run, the saved report's note,
the backtest switch and `tradingagents arena`. Every request is faked; nothing reaches the network."""

import copy
import io
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from requests.structures import CaseInsensitiveDict
from rich.console import Console
from typer.testing import CliRunner

import cli.arena as arena_cli
import cli.main as cli_main
import cli.run as cli_run
import tradingagents.default_config as default_config
from cli.models import AnalystType
from tradingagents.arena import ArenaError, ArenaLedger, alpharena, idempotency_key, mapping
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import _NOT_IN_SIGNATURE, TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog
from tradingagents.memory.settlement import Settlement
from tradingagents.reporting import write_report_tree

KEY = "aa_test_k3y-for-the-wiring-suite"
TODAY = "2026-10-29"
OPENED = {"trade_id": "abc-123-def-456", "status": "open", "filled_at": "2026-10-29T14:30:00Z",
          "fill_price": 195.5, "size_pct": 19.5, "position_value_usd": 1950, "warnings": ["MISSING_THESIS"]}
NOTE = ("Paper trade sent to AlphArena (simulated money, sandbox mode): opened a long in AAPL "
        "($1,950, trade abc-123-def-456).")


class _Response:
    """What ``requests.request(..., stream=True)`` returns, as far as the client reads it."""

    def __init__(self, status, payload=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict()
        self.body = b"" if payload is None else json.dumps(payload).encode()

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        pass


@pytest.fixture
def http(monkeypatch):
    """AlphArena's answers, queued on ``.answers``; ``.calls`` records each request."""
    faked = SimpleNamespace(calls=[], answers=[])

    def request(method, url, **kwargs):
        faked.calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        return faked.answers.pop(0)

    monkeypatch.setattr(alpharena.requests, "request", request)
    monkeypatch.setattr(alpharena.time, "sleep", lambda seconds: None)
    monkeypatch.setenv(alpharena.KEY_ENV, KEY)
    return faked


@pytest.fixture(autouse=True)
def _new_york_today(monkeypatch):
    """New York's date is fixed, so a run dated TODAY is a current-date run."""
    monkeypatch.setattr(mapping, "new_york_today", lambda now=None: date(2026, 10, 29))


@pytest.fixture(autouse=True)
def _reference_price(monkeypatch):
    """The latest close, as the data path would give it, without a download."""
    monkeypatch.setattr(mapping, "latest_close", lambda ticker, trade_date: (195.51, TODAY))


@pytest.fixture
def shown(monkeypatch):
    """What the CLI printed, through a real rich console: markup is parsed as in a terminal."""
    buffer = io.StringIO()
    monkeypatch.setattr(arena_cli, "console", Console(file=buffer, width=500, color_system=None))
    return lambda: buffer.getvalue()


@pytest.fixture
def asked(monkeypatch):
    """The questions typer.confirm was asked; the answers are queued on ``.answers``."""
    questions = SimpleNamespace(seen=[], answers=[])

    def confirm(text, default=None, **kwargs):
        questions.seen.append((text, default))
        answer = questions.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(arena_cli.typer, "confirm", confirm)
    return questions


def _state(rating="Buy", ticker="AAPL", trade_date=TODAY, asset_type="stock"):
    return {"company_of_interest": ticker, "trade_date": trade_date, "asset_type": asset_type,
            "final_rating": rating,
            "final_trade_decision": f"**Rating**: {rating}\n\n**Executive Summary**: Act.\n\n"
                                    "**Price Target**: 230.0\n\n**Confidence**: high",
            "trader_investment_plan": "**Action**: Buy\n\n**Stop Loss**: 181.0"}


def _config(tmp_path, **changes):
    return {**copy.deepcopy(DEFAULT_CONFIG), "arena_enabled": True,
            "arena_ledger_path": str(tmp_path / "arena" / "ledger.jsonl"), **changes}


def _no_client(*args, **kwargs):
    raise AssertionError("an AlphArena client was made")


# --- settings -------------------------------------------------------------------------------


def _config_with_env(monkeypatch, **env):
    for name in default_config._ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return default_config.build_default_config()


@pytest.mark.unit
def test_the_arena_is_off_by_default_with_the_documented_settings(monkeypatch):
    config = _config_with_env(monkeypatch)

    assert {key: value for key, value in config.items() if key.startswith("arena_")} == {
        "arena_enabled": False, "arena_mode": "sandbox", "arena_agent_id": None,
        "arena_buy_position_usd": 1950, "arena_overweight_position_usd": 1000,
        "arena_allow_short": False, "arena_send_thesis": False, "arena_ledger_path": None,
    }


@pytest.mark.unit
def test_the_defaults_are_the_ones_the_arena_package_assumes():
    """The config cannot import the package, so the values are copied; they must not drift."""
    assert set(default_config.ARENA_MODES) == set(alpharena.KEY_PREFIXES)
    assert DEFAULT_CONFIG["arena_buy_position_usd"] == mapping._SIZES["Buy"][1]
    assert DEFAULT_CONFIG["arena_overweight_position_usd"] == mapping._SIZES["Overweight"][1]


@pytest.mark.unit
def test_the_environment_switches_the_arena_on_and_names_the_mode_and_agent(monkeypatch):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_ARENA="1", TRADINGAGENTS_ARENA_MODE=" Live ",
                              TRADINGAGENTS_ARENA_AGENT_ID="my-agent")

    assert (config["arena_enabled"], config["arena_mode"], config["arena_agent_id"]) == (True, "live", "my-agent")


@pytest.mark.unit
@pytest.mark.parametrize("env_var, bad", [
    ("TRADINGAGENTS_ARENA_MODE", "production"),
    ("TRADINGAGENTS_ARENA_MODE", "test"),
    ("TRADINGAGENTS_ARENA", "maybe"),
])
def test_a_mistyped_arena_setting_fails_at_startup(monkeypatch, env_var, bad):
    """Otherwise it would surface only after the run, when the decision is offered."""
    monkeypatch.setenv(env_var, bad)
    with pytest.raises(ValueError, match=env_var):
        default_config.build_default_config()


def _bare_graph(config):
    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts = ("market", "news")
    graph.config = config
    return graph


@pytest.mark.unit
def test_arena_settings_change_neither_the_checkpoint_signature_nor_the_run_settings():
    base = copy.deepcopy(DEFAULT_CONFIG)
    graph = _bare_graph(base)
    signature, settings = graph._run_signature("stock"), graph.run_settings()

    for key, value in (("arena_enabled", True), ("arena_mode", "live"), ("arena_agent_id", "my-agent"),
                       ("arena_buy_position_usd", 500), ("arena_overweight_position_usd", 250),
                       ("arena_allow_short", True), ("arena_send_thesis", True),
                       ("arena_ledger_path", "/elsewhere/ledger.jsonl")):
        graph.config = {**base, key: value}
        assert graph._run_signature("stock") == signature, key
        assert graph.run_settings() == settings, key
    assert not [key for key in settings if "arena" in key]


@pytest.mark.unit
def test_a_checkpoint_saved_before_the_arena_settings_existed_still_resumes():
    arena_keys = {key for key in DEFAULT_CONFIG if key.startswith("arena_")}
    graph = _bare_graph(copy.deepcopy(DEFAULT_CONFIG))
    signature = graph._run_signature("stock")

    graph.config = {key: value for key, value in DEFAULT_CONFIG.items() if key not in arena_keys}

    assert arena_keys and arena_keys <= _NOT_IN_SIGNATURE
    assert graph._run_signature("stock") == signature


@pytest.mark.unit
def test_a_backtest_never_offers_a_cell_to_alpharena(monkeypatch, tmp_path):
    import tradingagents.backtest as bt

    built = []

    class Graph:
        def __init__(self, selected_analysts=None, config=None, **kwargs):
            built.append(config)
            self.memory_log = TradingMemoryLog(config)

        def propagate(self, *args, **kwargs):
            return {}, "Hold"

        def settle_pending(self, ticker):
            return Settlement()

    monkeypatch.setattr(bt, "TradingAgentsGraph", Graph)

    bt.run_backtest(["AAPL"], ["2026-01-05"], {"results_dir": str(tmp_path), "arena_enabled": True,
                                               "memory_log_path": str(tmp_path / "live.md")})

    assert built[0]["arena_enabled"] is False


# --- the analyze flags ------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("args, arena, arena_yes", [
    ([], None, False),
    (["--arena"], True, False),
    (["--no-arena"], False, False),
    (["--arena", "--arena-yes"], True, True),
])
def test_the_arena_flags_reach_the_run(monkeypatch, args, arena, arena_yes):
    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))

    result = CliRunner().invoke(cli_main.app, ["--ticker", "AAPL", "--save", "--no-show", *args])

    assert result.exit_code == 0, result.output
    assert (seen["flags"]["arena"], seen["flags"]["arena_yes"]) == (arena, arena_yes)


class _Buffer:
    """The live view's buffer, as the run drives it."""

    def __init__(self):
        self.messages, self.tool_calls, self.report_sections, self.agent_status = [], [], {}, {}
        self.selected_analysts, self._processed_message_ids = [], set()

    def init_for_analysis(self, analysts):
        self.selected_analysts = list(analysts)

    def add_message(self, kind, content):
        self.messages.append((0.0, kind, content))

    def add_tool_call(self, name, args):
        self.tool_calls.append((0.0, name, args))

    def update_report_section(self, *args):
        pass

    def update_agent_status(self, agent, status):
        self.agent_status[agent] = status


def _drive_run(monkeypatch, tmp_path, flags, note, env_enabled=False, selections=None, key=KEY):
    """Run the CLI's analysis against stand-ins; returns the order of events and what was seen.

    ``key`` is the (fake) sandbox key in the environment, None for none."""
    events, seen = [], {}
    if key is None:
        monkeypatch.delenv(alpharena.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(alpharena.KEY_ENV, key)

    class Graph:
        def __init__(self, *args, **kwargs):
            self.propagator = self

        def create_run_state(self, *args, **kwargs):
            return {"messages": []}

        def get_graph_args(self, callbacks=None):
            return {}

        def begin_checkpoint(self, *args, **kwargs):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"messages": [], **_state()}

        def record_decision(self, *args):
            events.append("decision recorded")

        def clear_checkpoint_on_success(self, *args):
            pass

        def end_checkpoint(self):
            pass

    class Live:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            events.append("live view opens")
            return self

        def __exit__(self, *args):
            events.append("live view closes")
            return False

    def offer(final_state, config, assume_yes=False):
        events.append("offer")
        seen.update(offered=final_state, config=config, assume_yes=assume_yes)
        return note

    def reports(final_state, graph, ticker, **kwargs):
        events.append("reports")
        seen["saved"] = final_state

    monkeypatch.setattr(cli_run, "TradingAgentsGraph", Graph)
    monkeypatch.setattr(cli_run, "Live", Live)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "message_buffer", _Buffer())
    monkeypatch.setattr(cli_run, "offer_paper_trade", offer)
    monkeypatch.setattr(cli_run, "_offer_reports", reports)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "AAPL", "analysis_date": TODAY, "analysts": [AnalystType.MARKET],
        "asset_type": "stock", "horizon": "swing", **(selections or {})})
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: {
        "results_dir": str(tmp_path), "arena_enabled": env_enabled})

    cli_run.run_analysis(flags=flags)
    return events, seen


@pytest.mark.unit
def test_the_offer_comes_after_the_live_view_and_its_note_reaches_the_saved_report(monkeypatch, tmp_path):
    events, seen = _drive_run(monkeypatch, tmp_path, {"arena": True, "arena_yes": True}, NOTE)

    assert events == ["live view opens", "decision recorded", "live view closes", "offer", "reports"]
    assert seen["config"]["arena_enabled"] is True and seen["assume_yes"] is True
    assert seen["saved"]["arena_note"] == NOTE
    assert seen["saved"]["final_trade_decision"] == seen["offered"]["final_trade_decision"]


@pytest.mark.unit
@pytest.mark.parametrize("flags, env_enabled, enabled", [
    ({}, False, False),                 # the default
    ({}, True, True),                   # TRADINGAGENTS_ARENA, through the config
    ({"arena": False}, True, False),    # --no-arena wins over it
])
def test_without_a_submission_the_report_is_saved_unchanged(monkeypatch, tmp_path, flags, env_enabled, enabled):
    events, seen = _drive_run(monkeypatch, tmp_path, flags, None, env_enabled=env_enabled)

    assert seen["config"]["arena_enabled"] is enabled and seen["assume_yes"] is False
    assert seen["saved"] is seen["offered"] and "arena_note" not in seen["saved"]


# --- the check before the run -----------------------------------------------------------------


@pytest.fixture
def no_request(monkeypatch):
    """The check before a run reads the key's prefix only; no request may be made."""
    def request(*args, **kwargs):
        raise AssertionError("a request was made before the run")

    monkeypatch.setattr(alpharena.requests, "request", request)


@pytest.mark.unit
@pytest.mark.parametrize("key, mode", [
    pytest.param(None, "sandbox", id="missing"),
    pytest.param("aa_live_f4ke-key-for-the-wiring-suite", "sandbox", id="live_key_in_sandbox_mode"),
    pytest.param(KEY, "live", id="sandbox_key_in_live_mode"),
])
def test_a_key_that_could_not_be_used_stops_the_run_before_it_starts(monkeypatch, tmp_path, shown, no_request,
                                                                     key, mode):
    if key is None:
        monkeypatch.delenv(alpharena.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(alpharena.KEY_ENV, key)

    with pytest.raises(typer.Exit) as stopped:
        arena_cli.check_before_run("AAPL", TODAY, _config(tmp_path, arena_mode=mode))

    assert stopped.value.exit_code == 2
    assert "ALPHARENA_API_KEY" in shown() and "--no-arena" in shown() and "has not started" in shown()
    assert key is None or key not in shown()          # the key itself is never shown


@pytest.mark.unit
def test_a_run_whose_decision_could_be_sent_starts_quietly_with_a_usable_key(monkeypatch, tmp_path, shown,
                                                                            no_request):
    monkeypatch.setenv(alpharena.KEY_ENV, KEY)
    assert arena_cli.check_before_run("AAPL", TODAY, _config(tmp_path)) is None
    assert shown() == ""


@pytest.mark.unit
@pytest.mark.parametrize("ticker, trade_date, changes, asset_type, said", [
    pytest.param("RELIANCE.NS", TODAY, {}, "stock", mapping.SEBI_REFUSAL, id="indian_listing"),
    pytest.param("AAPL", "2026-10-28", {}, "stock", "only a decision dated today in New York", id="past_date"),
    pytest.param("AAPL", TODAY, {"decision_horizon": "intraday"}, "stock", "swing decisions only", id="intraday"),
    pytest.param("BTC-USD", TODAY, {}, "crypto", "US stocks and ETFs only", id="crypto"),
])
def test_a_run_whose_decision_could_not_be_sent_says_why_and_goes_on(monkeypatch, tmp_path, shown, no_request,
                                                                     ticker, trade_date, changes, asset_type, said):
    monkeypatch.delenv(alpharena.KEY_ENV, raising=False)    # not needed, so not asked for

    assert arena_cli.check_before_run(ticker, trade_date, _config(tmp_path, **changes), asset_type=asset_type) is None
    assert said in shown() and "will not be sent" in shown() and len(shown().splitlines()) == 1


@pytest.mark.unit
def test_with_no_terminal_the_key_is_needed_only_with_arena_yes(monkeypatch, tmp_path, shown, no_request):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: False)
    monkeypatch.delenv(alpharena.KEY_ENV, raising=False)

    assert arena_cli.check_before_run("AAPL", TODAY, _config(tmp_path)) is None
    assert "no terminal" in shown() and "--arena-yes" in shown()
    with pytest.raises(typer.Exit) as stopped:
        arena_cli.check_before_run("AAPL", TODAY, _config(tmp_path), assume_yes=True)
    assert stopped.value.exit_code == 2


@pytest.mark.unit
def test_with_the_arena_off_nothing_is_checked(monkeypatch, shown):
    monkeypatch.delenv(alpharena.KEY_ENV, raising=False)
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _no_client)

    assert arena_cli.check_before_run("AAPL", TODAY, copy.deepcopy(DEFAULT_CONFIG)) is None
    assert shown() == ""


@pytest.mark.unit
def test_the_cli_checks_before_the_live_view_and_a_missing_key_spends_nothing(monkeypatch, tmp_path, shown):
    with pytest.raises(typer.Exit) as stopped:
        _drive_run(monkeypatch, tmp_path, {"arena": True}, None, key=None)
    assert stopped.value.exit_code == 2 and "ALPHARENA_API_KEY" in shown()

    # A decision that could not be sent: said once up front, and the run goes on as before.
    events, _ = _drive_run(monkeypatch, tmp_path, {"arena": True}, None, key=None,
                           selections={"ticker": "RELIANCE.NS"})
    assert mapping.SEBI_REFUSAL in shown()
    assert events == ["live view opens", "decision recorded", "live view closes", "offer", "reports"]


# --- the offer itself -------------------------------------------------------------------------


@pytest.mark.unit
def test_off_by_default_nothing_is_shown_asked_or_sent(monkeypatch, shown, asked):
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _no_client)

    assert arena_cli.offer_paper_trade(_state(), copy.deepcopy(DEFAULT_CONFIG)) is None
    assert shown() == "" and asked.seen == []


@pytest.mark.unit
def test_arena_yes_alone_does_not_switch_the_arena_on(monkeypatch, shown):
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _no_client)

    assert arena_cli.offer_paper_trade(_state(), copy.deepcopy(DEFAULT_CONFIG), assume_yes=True) is None
    assert "--arena-yes sent nothing" in shown()


@pytest.mark.unit
@pytest.mark.parametrize("state, changes, said", [
    pytest.param(_state(ticker="RELIANCE.NS"), {}, mapping.SEBI_REFUSAL, id="indian_listing"),
    pytest.param(_state(trade_date="2026-10-28"), {}, "only a decision dated today in New York", id="past_date"),
    pytest.param(_state(), {"decision_horizon": "intraday"}, "swing decisions only", id="intraday"),
    pytest.param(_state(ticker="BTC-USD", asset_type="crypto"), {}, "US stocks and ETFs only", id="crypto"),
    pytest.param(_state(ticker="^GSPC"), {}, "an index", id="index"),
])
def test_a_run_alpharena_cannot_take_is_turned_away_before_any_request(monkeypatch, tmp_path, shown, asked,
                                                                       state, changes, said):
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _no_client)

    assert arena_cli.offer_paper_trade(state, _config(tmp_path, **changes), assume_yes=True) is None

    assert said in shown() and "nothing sent" in shown()
    assert asked.seen == [] and not (tmp_path / "arena").exists()


@pytest.mark.unit
def test_with_no_terminal_and_no_arena_yes_nothing_is_sent(monkeypatch, tmp_path, shown, asked):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: False)
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _no_client)   # not even the open trades are read

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None
    assert "no terminal" in shown() and "--arena-yes" in shown() and asked.seen == []


@pytest.mark.unit
def test_the_trade_is_shown_then_sent_once_confirmed_and_recorded(http, tmp_path, shown, asked):
    http.answers += [_Response(200, {"trades": [], "pagination": {"total": 0}}), _Response(200, OPENED)]
    asked.answers.append(True)

    note = arena_cli.offer_paper_trade(_state(), _config(tmp_path))

    out = shown()
    assert "Open a long in AAPL: $1,950 at a reference price of 195.51 (close of 2026-10-29)" in out
    assert asked.seen == [("Send this paper trade to AlphArena (simulated money)?", False)]
    assert [(call.method, call.url) for call in http.calls] == [
        ("GET", f"{alpharena.BASE_URL}/trades"), ("POST", f"{alpharena.BASE_URL}/trades")]
    assert http.calls[1].json["idempotency_key"] == idempotency_key("AAPL", TODAY, "Buy")
    assert "✓ Opened a long in AAPL: trade abc-123-def-456; filled at 195.5 (AlphArena's price)" in out
    assert "MISSING_THESIS" in out
    entries = ArenaLedger(tmp_path / "arena" / "ledger.jsonl").entries()
    assert [(entry["action"], entry["response"]["trade_id"]) for entry in entries] == [("open_long", OPENED["trade_id"])]
    assert note == NOTE
    assert KEY not in out and KEY not in (tmp_path / "arena" / "ledger.jsonl").read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("answer", [False, typer.Abort()], ids=["no", "ctrl_c"])
def test_declining_sends_nothing_and_records_nothing(http, tmp_path, shown, asked, answer):
    http.answers.append(_Response(200, {"trades": []}))
    asked.answers.append(answer)

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None

    assert [call.method for call in http.calls] == ["GET"]
    assert ArenaLedger(tmp_path / "arena" / "ledger.jsonl").entries() == []
    assert "Nothing was sent to AlphArena." in shown()


@pytest.mark.unit
def test_arena_yes_sends_without_asking_even_with_no_terminal(monkeypatch, http, tmp_path, shown, asked):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: False)
    http.answers += [_Response(200, {"trades": []}), _Response(200, OPENED)]

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path), assume_yes=True) == NOTE
    assert asked.seen == [] and [call.method for call in http.calls] == ["GET", "POST"]


@pytest.mark.unit
def test_hold_shows_why_nothing_is_sent_and_asks_nothing(http, tmp_path, shown, asked):
    assert arena_cli.offer_paper_trade(_state("Hold"), _config(tmp_path)) is None

    assert "Hold keeps any open trade as it is; nothing is sent" in shown()
    assert http.calls == [] and asked.seen == []


@pytest.mark.unit
def test_a_sell_closes_the_open_long_and_says_what_alpharena_answered(http, tmp_path, shown, asked):
    held = {"trade_id": "t-1", "ticker": "AAPL", "direction": "long", "status": "open"}
    closed = {"trade_id": "t-1", "status": "closed", "exit_price": 210.25, "realized_pnl": 1475,
              "realized_pnl_pct": 7.55, "hold_duration": "5.2d"}
    http.answers += [_Response(200, {"trades": [held]}), _Response(200, closed)]
    asked.answers.append(True)

    note = arena_cli.offer_paper_trade(_state("Sell"), _config(tmp_path))

    assert http.calls[1].url == f"{alpharena.BASE_URL}/trades/t-1/close"
    assert ("✓ Closed the long in AAPL (trade t-1): exit at 210.25 (AlphArena's price); "
            "realized P&L 1,475 (7.55%)") in shown()
    assert note == ("Paper trade sent to AlphArena (simulated money, sandbox mode): closed the long in AAPL "
                    "(trade t-1).")


@pytest.mark.unit
def test_a_rejected_trade_is_reported_recorded_and_left_out_of_the_report(http, tmp_path, shown, asked):
    http.answers += [_Response(200, {"trades": []}),
                     _Response(400, {"error": {"code": "INSUFFICIENT_CASH", "message": "Not enough cash"}})]
    asked.answers.append(True)

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None

    assert ("✗ Could not open a long in AAPL: AlphArena INSUFFICIENT_CASH (HTTP 400): Not enough cash"
            in shown())
    entries = ArenaLedger(tmp_path / "arena" / "ledger.jsonl").entries()
    assert [entry["error"]["code"] for entry in entries] == ["INSUFFICIENT_CASH"]


@pytest.mark.unit
def test_a_decision_already_sent_is_not_opened_again(http, tmp_path, shown, asked):
    ledger = ArenaLedger(tmp_path / "arena" / "ledger.jsonl")
    ledger.append({"mode": "sandbox", "action": "open_long", "idempotency_key": idempotency_key("AAPL", TODAY, "Buy"),
                   "response": OPENED})
    http.answers.append(_Response(200, {"trades": []}))    # AlphArena has since closed it
    asked.answers.append(True)

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None

    assert [call.method for call in http.calls] == ["GET"]
    assert "Did not open a long in AAPL: already sent for this decision (see the ledger)" in shown()


@pytest.mark.unit
def test_a_missing_key_is_reported_and_nothing_is_sent(monkeypatch, http, tmp_path, shown, asked):
    monkeypatch.delenv(alpharena.KEY_ENV)

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None

    assert "MISSING_API_KEY" in shown() and "ALPHARENA_API_KEY" in shown()
    assert http.calls == [] and asked.seen == []


@pytest.mark.unit
def test_no_reference_price_means_nothing_is_offered(monkeypatch, http, tmp_path, shown, asked):
    def no_close(ticker, trade_date):
        raise NoMarketDataError(ticker, ticker, "no closing price to use as a reference price")

    monkeypatch.setattr(mapping, "latest_close", no_close)
    http.answers.append(_Response(200, {"trades": []}))

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None
    assert "no closing price" in shown() and asked.seen == [] and [c.method for c in http.calls] == ["GET"]


@pytest.mark.unit
def test_a_sell_closes_the_long_when_the_reference_price_cannot_be_read(monkeypatch, http, tmp_path,
                                                                         shown, asked):
    """With shorts on, only the short needs a price; the close goes ahead as with shorts off."""
    def rate_limited(ticker, trade_date):
        raise VendorUnavailableError("Yahoo Finance rate limited after 3 retries")

    monkeypatch.setattr(mapping, "latest_close", rate_limited)
    held = {"trade_id": "t-1", "ticker": "AAPL", "direction": "long", "status": "open"}
    http.answers += [_Response(200, {"trades": [held]}), _Response(200, {"trade_id": "t-1", "status": "closed"})]
    asked.answers.append(True)

    note = arena_cli.offer_paper_trade(_state("Sell"), _config(tmp_path, arena_allow_short=True))

    assert [(call.method, call.url) for call in http.calls] == [
        ("GET", f"{alpharena.BASE_URL}/trades"), ("POST", f"{alpharena.BASE_URL}/trades/t-1/close")]
    assert "No reference price for AAPL, so nothing is opened" in shown()
    assert "could not be read (Yahoo Finance rate limited after 3 retries)" in shown()
    assert note == ("Paper trade sent to AlphArena (simulated money, sandbox mode): "
                    "closed the long in AAPL (trade t-1).")


@pytest.mark.unit
def test_the_reference_price_is_read_through_the_runs_own_settings(monkeypatch, http, tmp_path, shown, asked):
    from tradingagents.dataflows.config import get_config

    read = []

    def latest_close(ticker, trade_date):
        read.append((ticker, trade_date, get_config()["data_cache_dir"]))
        return 195.51, TODAY

    monkeypatch.setattr(mapping, "latest_close", latest_close)
    http.answers.append(_Response(200, {"trades": []}))
    asked.answers.append(False)

    arena_cli.offer_paper_trade(_state(), _config(tmp_path, data_cache_dir=str(tmp_path / "run-cache")))

    assert read == [("AAPL", TODAY, str(tmp_path / "run-cache"))]


@pytest.mark.unit
def test_an_unwritable_ledger_means_nothing_is_sent(http, tmp_path, shown, asked):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    http.answers.append(_Response(200, {"trades": []}))

    config = _config(tmp_path, arena_ledger_path=str(blocker / "ledger.jsonl"))
    assert arena_cli.offer_paper_trade(_state(), config) is None

    assert "cannot be written" in shown() and asked.seen == []
    assert [call.method for call in http.calls] == ["GET"]


class _Exploding:
    """A client whose open fails in a way nobody planned for."""

    mode = "sandbox"

    def __init__(self, *args, **kwargs):
        pass

    def list_trades(self, status="open", **kwargs):
        return {"trades": []}

    def submit_trade(self, payload):
        raise RuntimeError(f"boom {KEY}")


@pytest.mark.unit
def test_an_unexpected_failure_never_reaches_the_run_or_shows_the_key(monkeypatch, tmp_path, shown, asked):
    monkeypatch.setenv(alpharena.KEY_ENV, KEY)
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _Exploding)
    asked.answers.append(True)

    assert arena_cli.offer_paper_trade(_state(), _config(tmp_path)) is None

    assert "unexpected RuntimeError" in shown() and "tradingagents arena trades" in shown()
    assert KEY not in shown()


@pytest.mark.unit
def test_ctrl_c_while_alpharena_answers_stops_only_this_step(monkeypatch, tmp_path, shown, asked):
    class Slow(_Exploding):
        def list_trades(self, status="open", **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(arena_cli, "AlphArenaClient", Slow)

    try:
        note = arena_cli.offer_paper_trade(_state(), _config(tmp_path))
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C at the AlphArena step ended the run before its report was offered")
    assert note is None and "AlphArena: interrupted" in shown() and asked.seen == []


@pytest.mark.unit
def test_alpharena_text_is_printed_as_text_never_as_markup(http, tmp_path, shown, asked):
    message = "[/bogus] [bold red]Not enough cash[/bold red] :warning:"
    http.answers += [_Response(200, {"trades": []}),
                     _Response(400, {"error": {"code": "INSUFFICIENT_CASH", "message": message}})]
    asked.answers.append(True)

    arena_cli.offer_paper_trade(_state(), _config(tmp_path))

    assert message in shown() and "unexpected" not in shown()


# --- the saved report --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_saved_report_notes_a_paper_trade_and_only_when_there_was_one(tmp_path):
    state = {"trade_date": TODAY, "market_report": "M", "final_trade_decision": "**Rating**: Buy"}

    write_report_tree(state, "AAPL", tmp_path / "plain")
    write_report_tree({**state, "arena_note": NOTE + " <b>&</b>"}, "AAPL", tmp_path / "sent")

    plain = (tmp_path / "plain" / "complete_report.md").read_text(encoding="utf-8")
    assert "AlphArena" not in plain and "- Mode: research and paper trading only; no orders are placed" in plain
    assert "AlphArena" not in (tmp_path / "plain" / "complete_report.html").read_text(encoding="utf-8")
    sent = (tmp_path / "sent" / "complete_report.md").read_text(encoding="utf-8")
    assert f"- {NOTE}" in sent and "- Mode: research and paper trading only; no real orders are placed" in sent
    page = (tmp_path / "sent" / "complete_report.html").read_text(encoding="utf-8")
    assert "Paper trade sent to AlphArena (simulated money, sandbox mode)" in page
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in page and "<b>&</b>" not in page


# --- tradingagents arena ------------------------------------------------------------------------


class _Arena:
    """A client double for the commands: answers from ``answers``, each call recorded."""

    mode = "sandbox"
    calls: list = []
    answers: dict = {}

    def __init__(self, mode=None):
        pass

    def _answer(self, name, *args):
        _Arena.calls.append((name, *args))
        answer = _Arena.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    def agent_profile(self, agent_id):
        return self._answer("agent_profile", agent_id)

    def agent_pnl(self, agent_id, period="all"):
        return self._answer("agent_pnl", agent_id, period)

    def agent_portfolio(self, agent_id):
        return self._answer("agent_portfolio", agent_id)

    def leaderboard(self, period="30d", sort="return", limit=20):
        return self._answer("leaderboard", period, sort, limit)

    def list_trades(self, status="open", **kwargs):
        return self._answer("list_trades", status)

    def close_trade(self, trade_id, reason=None):
        return self._answer("close_trade", trade_id, reason)


@pytest.fixture
def arena(monkeypatch, tmp_path):
    _Arena.calls, _Arena.answers = [], {}
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _Arena)
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("an analysis ran"))
    monkeypatch.setitem(DEFAULT_CONFIG, "arena_ledger_path", str(tmp_path / "ledger.jsonl"))
    return _Arena


def _invoke(*args, input=None):
    return CliRunner().invoke(cli_main.app, ["arena", *args], input=input)


@pytest.mark.unit
def test_the_arena_group_lists_its_commands_without_running_an_analysis(arena):
    result = _invoke("--help")

    assert result.exit_code == 0, result.output
    for command in ("status", "leaderboard", "trades", "close"):
        assert command in result.output


@pytest.mark.unit
def test_the_arena_group_is_registered_before_the_module_entry_point():
    """README documents `python -m cli.main`, which runs the file top to bottom."""
    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    assert source.index("app.add_typer(arena_app") < source.index('if __name__ == "__main__":')


@pytest.mark.unit
def test_status_needs_the_agent_id(arena, shown, monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "arena_agent_id", None)

    result = _invoke("status")

    assert result.exit_code == 1 and "TRADINGAGENTS_ARENA_AGENT_ID" in shown()
    assert arena.calls == []


@pytest.mark.unit
def test_status_shows_the_public_profile_pnl_and_positions(arena, shown, monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "arena_agent_id", "my-agent")
    arena.answers.update(
        agent_profile={"agent_id": "my-agent", "name": "[bold]My[/bold] Agent :rocket:", "status": "active",
                       "portfolio_value": 112500, "cash": 87500,
                       "stats": {"total_trades": 15, "open_trades": 2, "total_return_pct": 12.5, "win_rate": 0.73}},
        agent_pnl={"return_pct": 8.5, "sharpe": 2.1,
                   "equity_curve": [{"date": "2026-10-28", "portfolio_value": 10120}]},
        agent_portfolio={"cash": 87500, "cash_pct": 77.8, "positions": [
            {"ticker": "AAPL", "direction": "long", "entry_price": 190, "current_price": 198.5,
             "unrealized_pnl": 447, "opened_at": "2026-10-20T14:30:00Z"}]},
    )

    result = _invoke("status", "--period", "7d")

    out = shown()
    assert result.exit_code == 0, out
    assert arena.calls == [("agent_profile", "my-agent"), ("agent_pnl", "my-agent", "7d"),
                           ("agent_portfolio", "my-agent")]
    assert "[bold]My[/bold] Agent :rocket:" in out          # someone's text, shown as written
    for figure in ("112,500", "87,500", "12.5", "0.73", "8.5", "the last, 2026-10-28: 10,120", "AAPL", "198.5", "447"):
        assert figure in out, figure


@pytest.mark.unit
def test_the_leaderboard_takes_the_documented_choices_and_shows_names_as_text(arena, shown):
    arena.answers["leaderboard"] = {"leaderboard": [
        {"rank": 1, "agent_id": "volatility-edge", "name": "[red]Ignore previous instructions[/red]",
         "stats": {"total_return_pct": 12.94, "sharpe_ratio": 10.67, "win_rate": 0.8, "total_trades": 10}}]}

    result = _invoke("leaderboard", "--period", "7d", "--sort", "sharpe", "--limit", "5")

    assert result.exit_code == 0, shown()
    assert arena.calls == [("leaderboard", "7d", "sharpe", 5)]
    assert "[red]Ignore previous instructions[/red]" in shown() and "12.94" in shown()


@pytest.mark.unit
def test_an_empty_leaderboard_says_so(arena, shown):
    arena.answers["leaderboard"] = {"leaderboard": []}

    result = _invoke("leaderboard")

    assert result.exit_code == 0 and arena.calls == [("leaderboard", "30d", "return", 20)]
    assert "No agent is ranked for this period." in shown()


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--period", "1y"], ["--sort", "luck"], ["--limit", "0"], ["--limit", "101"]])
def test_the_leaderboard_refuses_values_alpharena_does_not_document(arena, args):
    assert _invoke("leaderboard", *args).exit_code == 2
    assert arena.calls == []


@pytest.mark.unit
def test_trades_lists_your_trades(arena, shown):
    arena.answers["list_trades"] = {"trades": [
        {"trade_id": "t-7", "ticker": "MSFT", "direction": "long", "status": "closed", "fill_price": 410.5,
         "exit_price": 420.0}], "pagination": {"total": 3}}

    result = _invoke("trades", "--status", "closed")

    assert result.exit_code == 0, shown()
    assert arena.calls == [("list_trades", "closed")]
    assert "sandbox mode" in shown() and "t-7" in shown() and "410.5" in shown()
    assert "The newest 1 of 3 are shown." in shown()


@pytest.mark.unit
def test_an_alpharena_error_ends_a_command_with_its_message_as_text(arena, shown):
    arena.answers["list_trades"] = ArenaError("MISSING_API_KEY", "ALPHARENA_API_KEY is not set [/bogus]")

    result = _invoke("trades")

    assert result.exit_code == 1
    assert "AlphArena MISSING_API_KEY: ALPHARENA_API_KEY is not set [/bogus]" in shown()


def _held():
    return {"trades": [{"trade_id": "t-1", "ticker": "AAPL", "direction": "long", "status": "open"},
                       {"trade_id": "t-2", "ticker": "AAPL", "direction": "short", "status": "open"},
                       {"trade_id": "t-3", "ticker": "MSFT", "direction": "long", "status": "open"}]}


@pytest.mark.unit
def test_close_shows_the_trades_asks_then_closes_and_records_them(arena, shown, monkeypatch, tmp_path):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: True)
    arena.answers.update(list_trades=_held(), close_trade={"status": "closed", "realized_pnl": -12.5})

    result = _invoke("close", "aapl", input="y\n")

    assert result.exit_code == 0, shown()
    assert "Close these paper trades on AlphArena (simulated money)? [y/N]" in result.output
    assert "Close the open long in AAPL (trade t-1)" in shown()
    assert arena.calls == [("list_trades", "open"), ("close_trade", "t-1", "manual"), ("close_trade", "t-2", "manual")]
    entries = ArenaLedger(tmp_path / "ledger.jsonl").entries()
    assert [(entry["action"], entry["rating"]) for entry in entries] == [("close_long", "manual"),
                                                                         ("close_short", "manual")]
    assert "realized P&L -12.5" in shown()


@pytest.mark.unit
@pytest.mark.parametrize("at_terminal, args, input, closed, code", [
    (True, [], "n\n", False, 0),          # declined
    (True, [], "\n", False, 0),           # no is the default
    (False, [], None, False, 1),          # nobody to ask
    (False, ["--yes"], None, True, 0),    # asked beforehand
])
def test_close_closes_only_what_the_person_confirmed(arena, shown, monkeypatch, at_terminal, args, input,
                                                    closed, code):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: at_terminal)
    arena.answers.update(list_trades=_held(), close_trade={"status": "closed"})

    result = _invoke("close", "AAPL", *args, input=input)

    assert result.exit_code == code, shown()
    assert any(call[0] == "close_trade" for call in arena.calls) is closed


@pytest.mark.unit
def test_close_refuses_an_indian_listing_before_any_request(arena, shown):
    result = _invoke("close", "RELIANCE.NS")

    assert result.exit_code == 1 and mapping.SEBI_REFUSAL in shown()
    assert arena.calls == []


@pytest.mark.unit
def test_a_failed_close_is_reported_and_ends_the_command_with_an_error(arena, shown, monkeypatch):
    monkeypatch.setattr(arena_cli, "_at_terminal", lambda: True)
    arena.answers.update(list_trades={"trades": [_held()["trades"][0]]},
                         close_trade=ArenaError("MARKET_PRICE_UNAVAILABLE", "Current market price is unavailable", 502))

    result = _invoke("close", "AAPL", "--yes")

    assert result.exit_code == 1
    assert ("✗ Could not close the long in AAPL (trade t-1): AlphArena MARKET_PRICE_UNAVAILABLE (HTTP 502): "
            "Current market price is unavailable") in shown()
