import math
import os
from datetime import time

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_DEEP_THINK_PROVIDER":       "deep_think_provider",
    "TRADINGAGENTS_QUICK_THINK_PROVIDER":      "quick_think_provider",
    "TRADINGAGENTS_DEEP_THINK_BACKEND_URL":    "deep_think_backend_url",
    "TRADINGAGENTS_QUICK_THINK_BACKEND_URL":   "quick_think_backend_url",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_MAX_TOOL_ROUNDS":      "max_tool_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    "TRADINGAGENTS_MAX_TOKENS":           "max_tokens",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
    # Indian market settings.
    "TRADINGAGENTS_DEFAULT_EXCHANGE":     "default_exchange",
    "TRADINGAGENTS_COST_PROFILE":         "cost_profile",
    "TRADINGAGENTS_RISK_PER_TRADE_PCT":   "risk_per_trade_pct",
    "TRADINGAGENTS_MAX_POSITION_PCT":     "max_position_pct",
    # Agent-Reach web research (docs/agent-reach.md).
    "TRADINGAGENTS_AGENT_REACH":          "agent_reach_enabled",
    "TRADINGAGENTS_AGENT_REACH_COMMAND":  "agent_reach_command",
    "TRADINGAGENTS_AGENT_REACH_HOME":     "agent_reach_home",
    # Intraday (day-trader) mode (docs/intraday.md).
    "TRADINGAGENTS_DECISION_HORIZON":     "decision_horizon",
    "TRADINGAGENTS_INTRADAY_SQUARE_OFF":  "intraday_square_off",
    "TRADINGAGENTS_INTRADAY_CAPITAL":     "intraday_paper_capital",
    "TRADINGAGENTS_INTRADAY_RISK_PCT":    "intraday_risk_pct",
    "TRADINGAGENTS_INTRADAY_LEVERAGE_CAP": "intraday_leverage_cap",
    "TRADINGAGENTS_INTRADAY_COST_PROFILE": "intraday_cost_profile",
    # AlphArena paper trading (docs/arena.md); the key itself is read only from ALPHARENA_API_KEY.
    "TRADINGAGENTS_ARENA":                "arena_enabled",
    "TRADINGAGENTS_ARENA_MODE":           "arena_mode",
    "TRADINGAGENTS_ARENA_AGENT_ID":       "arena_agent_id",
}


# What a run decides: a position rating over the holding period ("swing"), or a
# paper plan for one trading session ("intraday", docs/intraday.md).
DECISION_HORIZONS = ("swing", "intraday")

# AlphArena's two kinds of key: "sandbox" takes an aa_test_ key, "live" an aa_live_ one.
ARENA_MODES = ("sandbox", "live")


def _is_time_of_day(value: str) -> bool:
    try:
        return time.fromisoformat(value.strip()).tzinfo is None
    except ValueError:
        return False


def _is_cost_profile(value: str) -> bool:
    # Imported only when the variable is set; costs is pure (no vendor, no network).
    from tradingagents.markets.costs import COST_PROFILES

    return value in COST_PROFILES


# Overrides read without regard to case or surrounding spaces.
_ENV_NORMALIZE = {
    "decision_horizon": lambda v: v.strip().lower(),
    "arena_mode": lambda v: v.strip().lower(),
    "cost_profile": lambda v: v.strip().lower(),
    "intraday_cost_profile": lambda v: v.strip().lower(),
}

# Overrides whose value must also be in range, checked at startup like the type:
# a mistyped square-off or a zero leverage cap would otherwise leave every
# intraday snapshot unavailable for the whole run, a mistyped arena mode
# would surface only once the run had finished, and a mistyped charges
# profile would stop every run only after its setup questions.
_ENV_CHECKS = {
    "decision_horizon": (lambda v: v in DECISION_HORIZONS, "swing or intraday"),
    "arena_mode": (lambda v: v in ARENA_MODES, "sandbox or live"),
    "cost_profile": (_is_cost_profile, "a profile in tradingagents/markets/costs.py, "
                                       "e.g. india_equity_delivery or none"),
    "intraday_square_off": (_is_time_of_day, "a time as HH:MM"),
    "intraday_paper_capital": (lambda v: math.isfinite(v) and v > 0, "a positive amount"),
    "intraday_risk_pct": (lambda v: math.isfinite(v) and 0 < v <= 100, "a percentage above 0 and at most 100"),
    "intraday_leverage_cap": (lambda v: math.isfinite(v) and v > 0, "a positive multiple"),
    "intraday_cost_profile": (_is_cost_profile, "a profile in tradingagents/markets/costs.py, "
                                                "e.g. india_equity_intraday or none"),
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
        if key in _ENV_NORMALIZE:
            config[key] = _ENV_NORMALIZE[key](config[key])
        check = _ENV_CHECKS.get(key)
        if check is not None and not check[0](config[key]):
            raise ValueError(f"Invalid value for {env_var}: expected {check[1]}, got {raw!r}")
    return config


# The news chain while Agent-Reach is off, and while it is on: Agent-Reach first,
# Yahoo when it is missing or fails.
_NEWS_VENDORS = "yfinance"
AGENT_REACH_NEWS_VENDORS = "agent_reach,yfinance"


def with_agent_reach_news(config: dict, untouched: str = _NEWS_VENDORS) -> dict:
    """``config`` with news read through Agent-Reach first when agent_reach_enabled is
    on and news_data is still ``untouched`` (the value the defaults gave it); else
    ``config`` itself. A news_data chosen in a config or with --vendor is kept.

    Switched off in a config whose defaults the environment switched on, a news_data
    still at that switched default reads yfinance again, so Agent-Reach is not started.
    """
    vendors = config.get("data_vendors") or {}
    news = vendors.get("news_data")
    if not config.get("agent_reach_enabled"):
        if untouched == AGENT_REACH_NEWS_VENDORS and news == untouched:
            return {**config, "data_vendors": {**vendors, "news_data": _NEWS_VENDORS}}
        return config
    if news != untouched or news == AGENT_REACH_NEWS_VENDORS:
        return config
    return {**config, "data_vendors": {**vendors, "news_data": AGENT_REACH_NEWS_VENDORS}}


def build_default_config() -> dict:
    """The built-in defaults with the TRADINGAGENTS_* environment folded in.

    Read when the package is imported, as DEFAULT_CONFIG; call it again to see
    the environment as it is now.
    """
    return with_agent_reach_news(_apply_env_overrides({
        "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR") or os.path.join(_TRADINGAGENTS_HOME, "logs"),
        "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR") or os.path.join(_TRADINGAGENTS_HOME, "cache"),
        "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH") or os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md"),
        # Optional cap on the number of resolved memory log entries. When set,
        # the oldest resolved entries are pruned once this limit is exceeded.
        # Pending entries are never pruned. None disables rotation entirely.
        "memory_log_max_entries": None,
        # LLM settings
        "llm_provider": "openai",
        "deep_think_llm": "gpt-6-sol",
        "quick_think_llm": "gpt-6-luna",
        # When None, each provider's client falls back to its own default endpoint
        # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
        # The CLI overrides this per provider when the user picks one. Keeping a
        # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
        # being forwarded to Gemini, producing malformed request URLs).
        "backend_url": None,
        # A tier may name its own provider and endpoint (#1440): the quick tier serves
        # the analysts, researchers, debaters and trader, the deep tier the managers.
        # None means the tier uses llm_provider and backend_url.
        "quick_think_provider": None,
        "deep_think_provider": None,
        "quick_think_backend_url": None,
        "deep_think_backend_url": None,
        # Provider-specific thinking configuration
        "google_thinking_level": None,      # "high", "minimal", etc.
        "openai_reasoning_effort": None,    # "medium", "high", "low"
        "anthropic_effort": None,           # "high", "medium", "low"
        # Sampling temperature, forwarded to every provider when set. None leaves
        # each provider at its own default. Lower values reduce run-to-run
        # variation on models that honor it; reasoning models largely ignore it
        # and no setting makes LLM output bit-identical across runs (see README).
        "temperature": None,
        # SDK retry budget forwarded to every provider chat client. None leaves each
        # provider/SDK at its own default (usually 2). Raise it to ride out bursty
        # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
        "llm_max_retries": None,
        # Cap on output tokens forwarded to every provider chat client. None leaves
        # each provider at its own default. Set it to bound a model that emits
        # unbounded reasoning/output and hangs or trips a gateway idle timeout
        # (e.g. some deepseek-v4-flash deployments, #1204).
        "max_tokens": None,
        # Checkpoint/resume: when True, LangGraph saves state after each node
        # so a crashed run can resume from the last successful step.
        "checkpoint_enabled": False,
        # Output language for analyst reports and final decision
        # Internal agent debate stays in English for reasoning quality
        "output_language": "English",
        # Debate and discussion settings
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 100,
        # Rounds of tool calls an analyst may make before it is asked for its report.
        "max_tool_rounds": 20,
        # News / data fetching parameters
        # Increase for longer lookback strategies or to broaden macro coverage;
        # decrease to reduce token usage in agent prompts.
        "news_article_limit": 20,             # max articles per ticker (ticker-news)
        "global_news_article_limit": 10,      # max articles for global/macro news
        "global_news_lookback_days": 7,       # macro news lookback window
        # Search queries used by get_global_news for macro headlines. Extend or
        # replace to broaden geographic / sector coverage.
        "global_news_queries": [
            "Federal Reserve interest rates inflation",
            "S&P 500 earnings GDP economic outlook",
            "geopolitical risk trade war sanctions",
            "ECB Bank of England BOJ central bank policy",
            "oil commodities supply chain energy",
        ],
        # For an NSE/BSE listing, get_global_news reports two groups: India, from
        # these searches, and Global, from global_news_queries. Yahoo's fuzzy
        # search answers multi-word India queries with unrelated US stories, so
        # these are short and searched exactly; the sparse ones go first, since
        # the searches stop once enough articles are found.
        "global_news_queries_india": ["rupee", "RBI", "India"],
        "global_news_india_fuzzy": False,
        # An India-group article is kept only when its title or summary mentions
        # one of these terms (whole words, case-insensitive), or two of the weak
        # terms, or Yahoo tags it with an NSE/BSE listing, an Indian index or the
        # rupee. A weak term alone also names things outside India (the Pakistani
        # rupee, Raiffeisen's RBI, the BSE of other exchanges), so it needs a second.
        "global_news_india_terms": [
            "India", "Indian", "Indians", "SEBI", "Sensex", "Dalal Street", "Mumbai", "Delhi",
        ],
        "global_news_india_weak_terms": ["rupee", "RBI", "Nifty", "NSE", "BSE"],
        # Data vendor configuration
        # Category-level configuration (default for all tools in category).
        # The configured value is the exact vendor chain — requests are NOT silently
        # routed to vendors you didn't choose. For ordered fallback, list several,
        # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
        "data_vendors": {
            "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
            "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
            # Statements come from SEC EDGAR as filed (US filers), then Yahoo; the
        # overview and insider tools, which SEC EDGAR does not serve, from Yahoo.
        "fundamental_data": "sec_edgar,yfinance",  # Options: sec_edgar, alpha_vantage, yfinance
            # With agent_reach_enabled on, this default reads "agent_reach,yfinance".
            "news_data": "yfinance",             # Options: alpha_vantage, yfinance, agent_reach
            "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
            "prediction_markets": "polymarket",  # Options: polymarket (keyless)
            "market_context": "yfinance",        # Options: yfinance (benchmarks, sector index, INR, crude)
            "corporate_actions": "yfinance",     # Options: yfinance (dividends, splits, event calendar)
            # A category not set here uses every vendor that serves it:
            # "intraday_data": "yfinance"        # Options: yfinance (intraday bars, levels, paper sizing)
            # "web_research": "agent_reach"      # Options: agent_reach (web pages, YouTube videos and
            #                                    # captions); left unset, used only with agent_reach_enabled on
        },
        # Tool-level configuration (takes precedence over category-level)
        "tool_vendors": {
            # Example: "get_stock_data": "alpha_vantage",  # Override category default
        },
        # Benchmark for alpha calculation in the reflection layer.
        # ``benchmark_ticker`` (when set, e.g. by --benchmark) measures the swing
        # decisions recorded while it is set: each decision stores its benchmark,
        # and it is checked before the run. Leave it None to use the exchange's
        # headline index for NSE/BSE listings and ``benchmark_map`` by suffix
        # otherwise. SPY remains the US default
        # so the reflection label keeps reading "Alpha vs SPY" for US tickers
        # while non-US tickers get their regional index automatically.
        # Trading days from the first close after the analysis date over which a
        # decision's outcome is measured, for reflection and for the backtest figures.
        "holding_period_days": 5,
        # Portfolio risk figures given to the trader, risk and portfolio agents when a
        # portfolio is passed (--portfolio): per-position volatility, drawdown and beta,
        # sector exposure, concentration, and a sizing reference that risks
        # risk_per_trade_pct of the book with a 2 x ATR stop, capped at max_position_pct.
        "portfolio_risk": True,
        "risk_per_trade_pct": 1.0,
        "max_position_pct": 20.0,
        # Example charges and tax for NSE/BSE trades (tradingagents/markets/costs.py).
        # Rates are dated examples: override any field, e.g.
        # {"brokerage_rate": 0.0003, "brokerage_cap": 20.0}; "none" applies no charges.
        "cost_profile": "india_equity_delivery",
        "cost_overrides": {},
        "capital_gains_rules": "india_listed_equity",
        "capital_gains_overrides": {},
        # Exchange a bare ticker (RELIANCE) is listed on: "NSE" or "BSE"; None leaves
        # it bare, as for a US listing. The CLI's --exchange sets it for one run.
        "default_exchange": None,
        # Named symbol lists for `tradingagents market --watchlist` and `backtest
        # --watchlist`: here, and in a JSON file mapping names to symbol lists.
        "watchlists": {},
        "watchlists_file": os.getenv("TRADINGAGENTS_WATCHLISTS_FILE") or os.path.join(_TRADINGAGENTS_HOME, "watchlists.json"),
        "benchmark_ticker": None,
        "benchmark_map": {
            ".NS":  "^NSEI",       # NSE India (Nifty 50)
            ".BO":  "^BSESN",      # BSE India (Sensex)
            ".T":   "^N225",       # Tokyo (Nikkei 225)
            ".TW":  "^TWII",       # Taiwan (TAIEX)
            ".TWO": "^TWII",       # Taipei OTC (TPEx has no Yahoo index; TAIEX)
            ".KS":  "^KS11",       # Korea (KOSPI)
            ".KQ":  "^KQ11",       # Korea (KOSDAQ)
            ".HK":  "^HSI",        # Hong Kong (Hang Seng)
            ".SI":  "^STI",        # Singapore (Straits Times)
            ".L":   "^FTSE",       # London (FTSE 100)
            ".DE":  "^GDAXI",      # Germany (DAX)
            ".PA":  "^FCHI",       # Paris (CAC 40)
            ".AS":  "^AEX",        # Amsterdam (AEX)
            ".SW":  "^SSMI",       # Switzerland (SMI)
            ".MI":  "FTSEMIB.MI",  # Milan (FTSE MIB)
            ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
            ".AX":  "^AXJO",       # Australia (ASX 200)
            ".SS":  "000001.SS",   # Shanghai (SSE Composite)
            ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
            ".SA":  "^BVSP",       # B3 Brazil (Ibovespa)
            "":     "SPY",         # default for US-listed tickers (no suffix)
        },
        # Agent-Reach (docs/agent-reach.md): Exa web search, Jina Reader and YouTube
        # through the separately installed agent-reach CLI, no-login channels only.
        # Off by default. On, news_data's default chain reads Agent-Reach first (Yahoo
        # when it is missing or fails) and the News Analyst can also read web pages
        # and videos (the web_research tools).
        "agent_reach_enabled": False,
        "agent_reach_command": "agent-reach",     # a program name or path, not a command line
        "agent_reach_home": "~/.tradingagents/agent-reach-home",   # its own HOME, created 0700
        "agent_reach_timeout": 30,                # seconds per call
        "agent_reach_max_results": 5,             # search results asked for (news asks for twice this)
        "agent_reach_snippet_chars": 600,
        "agent_reach_page_chars": 6000,
        "agent_reach_transcript_chars": 8000,
        "agent_reach_video_lookback_days": 30,    # get_video_coverage's window
        # Searches for the India section of global news for an NSE/BSE listing. Exa's
        # semantic search suits multi-word topics, unlike Yahoo's keyword search.
        "agent_reach_india_queries": [
            "RBI monetary policy repo rate",
            "Nifty Sensex Indian stock market",
            "FII DII flows Indian equities",
            "rupee dollar exchange rate",
            "India inflation CPI",
            "SEBI regulation markets",
            "India Union Budget economy",
            "crude oil prices India",
        ],
        # Intraday (day-trader) mode (docs/intraday.md); plans are paper only.
        # "swing" rates the position over the holding period. "intraday" adds the Day
        # Trader analyst, has the Trader and Portfolio Manager write a plan for one
        # session, and scores that plan on the session's 5m bars once its close is final.
        "decision_horizon": "swing",
        # A request whose cut-off is in a trading session refetches bars older than this.
        "intraday_cache_ttl_seconds": 60,
        # When NSE/BSE intraday (MIS) positions are squared off, HH:MM IST: Upstox and
        # Angel One 15:10, Zerodha and ICICI Direct 15:12 for F&O stocks (Oct 2026); some
        # brokers, e.g. Groww, square off earlier. Other listings exit by the session close.
        "intraday_square_off": "15:10",
        # Paper notional for the sizing examples, in the listing's currency; a run given
        # a portfolio (--portfolio) sizes against the portfolio's value instead.
        "intraday_paper_capital": 100000.0,
        "intraday_risk_pct": 0.5,                 # percent of the capital risked per paper trade
        # 1.0 is no leverage. Brokers allow up to 5x on MIS under SEBI's 20% margin
        # rule; raise it only knowingly.
        "intraday_leverage_cap": 1.0,
        # Charges NSE/BSE intraday plans are sized and scored with (tradingagents/markets/costs.py).
        # The rates are dated examples: override any field for your broker, e.g.
        # {"brokerage_rate": 0.0, "brokerage_cap": None}; "none" applies no charges.
        # Other listings carry no charges.
        "intraday_cost_profile": "india_equity_intraday",
        "intraday_cost_overrides": {},
        # AlphArena paper trading (docs/arena.md), off by default: after a CLI run, a swing
        # decision on a US stock or ETF dated today in New York is offered to AlphArena as a
        # paper trade with simulated money, and sent only once you confirm it (or without
        # asking with --arena-yes). Indian listings are never sent. None of these settings
        # changes what a run decides.
        "arena_enabled": False,
        "arena_mode": "sandbox",                  # "sandbox" takes an aa_test_ key, "live" an aa_live_ key
        "arena_agent_id": None,                   # your AlphArena agent id, for `tradingagents arena status`
        "arena_buy_position_usd": 1950,           # under the $2,000 from which AlphArena requires a public thesis
        "arena_overweight_position_usd": 1000,
        "arena_allow_short": False,               # a Sell also opens a short of arena_buy_position_usd
        "arena_send_thesis": False,               # the executive summary (<=300 chars), public and locked once sent
        "arena_ledger_path": None,                # None: <results_dir>/arena/ledger.jsonl
    }))


DEFAULT_CONFIG = build_default_config()
