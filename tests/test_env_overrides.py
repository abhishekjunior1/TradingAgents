"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import os

import pytest

import tradingagents.default_config as default_config_module


def _config_with_env(monkeypatch, **overrides):
    """The defaults as the environment given here would set them."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return default_config_module.build_default_config()


def test_no_env_uses_built_in_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["llm_provider"] == "openai"
    assert config["deep_think_llm"] == "gpt-6-sol"
    assert config["quick_think_llm"] == "gpt-6-luna"
    assert config["backend_url"] is None
    assert config["max_debate_rounds"] == 1
    assert config["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert config["llm_provider"] == "google"
    assert config["deep_think_llm"] == "gemini-3-pro-preview"
    assert config["quick_think_llm"] == "gemini-3-flash-preview"
    assert config["backend_url"] == "https://example.invalid/v1"
    assert config["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert config["max_debate_rounds"] == 3
    assert isinstance(config["max_debate_rounds"], int)
    assert config["max_risk_discuss_rounds"] == 2
    assert isinstance(config["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert config["checkpoint_enabled"] is expected


def test_reasoning_thinking_overrides(monkeypatch):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert config["openai_reasoning_effort"] == "high"
    assert config["google_thinking_level"] == "minimal"
    assert config["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    config = _config_with_env(monkeypatch)
    assert config["openai_reasoning_effort"] is None
    assert config["google_thinking_level"] is None
    assert config["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert config["llm_provider"] == "openai"
    assert config["max_debate_rounds"] == 1


def test_empty_path_value_keeps_the_default_path(monkeypatch):
    """.env.example lists the path variables blank; uncommenting one made the
    path empty, and the graph failed creating its directories."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_RESULTS_DIR="",
        TRADINGAGENTS_CACHE_DIR="",
        TRADINGAGENTS_MEMORY_LOG_PATH="",
    )
    home = default_config_module._TRADINGAGENTS_HOME
    assert config["results_dir"] == os.path.join(home, "logs")
    assert config["data_cache_dir"] == os.path.join(home, "cache")
    assert config["memory_log_path"] == os.path.join(home, "memory", "trading_memory.md")


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        default_config_module.build_default_config()


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(monkeypatch, bad):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_ENABLED", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_CHECKPOINT_ENABLED"):
        default_config_module.build_default_config()


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in config


# --- Agent-Reach and intraday settings ------------------------------------------

def test_agent_reach_and_intraday_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["agent_reach_enabled"] is False
    assert config["agent_reach_command"] == "agent-reach"
    assert config["agent_reach_home"] == "~/.tradingagents/agent-reach-home"
    assert config["data_vendors"]["news_data"] == "yfinance"
    assert "web_research" not in config["data_vendors"] and "intraday_data" not in config["data_vendors"]
    assert (config["intraday_square_off"], config["intraday_paper_capital"], config["intraday_risk_pct"],
            config["intraday_leverage_cap"], config["intraday_cache_ttl_seconds"]) == ("15:10", 100000.0, 0.5, 1.0, 60)
    assert (config["intraday_cost_profile"], config["intraday_cost_overrides"]) == ("india_equity_intraday", {})


def test_agent_reach_india_queries_match_the_vendors_default(monkeypatch):
    # The config cannot import the vendor, so the list is copied; it must not drift.
    from tradingagents.dataflows.vendors.agent_reach.news import INDIA_QUERIES

    assert _config_with_env(monkeypatch)["agent_reach_india_queries"] == INDIA_QUERIES


def test_agent_reach_overrides(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_AGENT_REACH="1",
        TRADINGAGENTS_AGENT_REACH_COMMAND="/opt/agent-reach/bin/agent-reach",
        TRADINGAGENTS_AGENT_REACH_HOME="/srv/agent-reach-home",
    )
    assert config["agent_reach_enabled"] is True
    assert config["agent_reach_command"] == "/opt/agent-reach/bin/agent-reach"
    assert config["agent_reach_home"] == "/srv/agent-reach-home"
    # Switched on, the default news chain reads Agent-Reach first and Yahoo after it.
    assert config["data_vendors"]["news_data"] == "agent_reach,yfinance"


def test_intraday_overrides_are_coerced(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_INTRADAY_SQUARE_OFF="15:12",
        TRADINGAGENTS_INTRADAY_CAPITAL="250000.5",
        TRADINGAGENTS_INTRADAY_RISK_PCT="1",
        TRADINGAGENTS_INTRADAY_LEVERAGE_CAP="5",
        TRADINGAGENTS_INTRADAY_COST_PROFILE=" None ",
    )
    assert config["intraday_square_off"] == "15:12"
    assert config["intraday_paper_capital"] == 250000.5
    assert config["intraday_risk_pct"] == 1.0 and isinstance(config["intraday_risk_pct"], float)
    assert config["intraday_leverage_cap"] == 5.0
    assert config["intraday_cost_profile"] == "none"


@pytest.mark.parametrize("env_var, bad", [
    ("TRADINGAGENTS_INTRADAY_SQUARE_OFF", "3:10pm"),
    ("TRADINGAGENTS_INTRADAY_SQUARE_OFF", "25:00"),
    ("TRADINGAGENTS_INTRADAY_SQUARE_OFF", "15:10+05:30"),
    ("TRADINGAGENTS_INTRADAY_CAPITAL", "0"),
    ("TRADINGAGENTS_INTRADAY_CAPITAL", "inf"),
    ("TRADINGAGENTS_INTRADAY_CAPITAL", "1e5 INR"),
    ("TRADINGAGENTS_INTRADAY_RISK_PCT", "-0.5"),
    ("TRADINGAGENTS_INTRADAY_RISK_PCT", "150"),
    ("TRADINGAGENTS_INTRADAY_RISK_PCT", "nan"),
    ("TRADINGAGENTS_INTRADAY_LEVERAGE_CAP", "0"),
    ("TRADINGAGENTS_INTRADAY_COST_PROFILE", "zerodha"),
    ("TRADINGAGENTS_AGENT_REACH", "enabled"),
])
def test_invalid_intraday_and_agent_reach_values_raise(monkeypatch, env_var, bad):
    """A mistyped square-off or a zero leverage cap would leave every intraday
    snapshot unavailable; it fails at startup instead, naming the variable."""
    monkeypatch.setenv(env_var, bad)
    with pytest.raises(ValueError, match=env_var):
        default_config_module.build_default_config()
