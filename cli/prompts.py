import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

import questionary
import typer
from dotenv import find_dotenv, set_key
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS, SIGNED_IN_PROVIDERS

TICKER_INPUT_EXAMPLES = "RELIANCE.NS, TCS.BO, NIFTY 50, SPY, BTC-USD"

ANALYST_CHOICES = [
    ("Market Analyst", AnalystType.MARKET),
    ("Sentiment Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
    ("Day Trader (intraday plan)", AnalystType.DAY_TRADER),
]

# The analysts a run gets when none are named: every one but the Day Trader,
# which an intraday run adds by itself.
DEFAULT_ANALYSTS = [analyst for _, analyst in ANALYST_CHOICES if analyst != AnalystType.DAY_TRADER]

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


# The style of every selection menu.
_MENU_STYLE = questionary.Style([
    ("selected", "fg:magenta noinherit"),
    ("highlighted", "fg:magenta noinherit"),
    ("pointer", "fg:magenta noinherit"),
])


def is_valid_ticker_input(value: str) -> bool:
    """Whether a ticker entry names a symbol the run can use.

    The symbol it resolves to (``XAUUSD+`` -> ``GC=F``) must pass the data
    layer's own check, so the prompt refuses exactly what the run would.
    Empty input is allowed (it defaults to SPY downstream).
    """
    if not value.strip():
        return True
    try:
        safe_ticker_component(normalize_ticker_symbol(value))
    except ValueError:
        return False
    return True


def get_ticker() -> str:
    """Prompt the user to enter a ticker symbol, preserving exchange suffixes.

    Uses questionary.text (not typer.prompt, which strips trailing dot-suffixes
    like ``000404.SH`` on some shells) and validates the symbol charset so an
    obvious typo is caught before the run starts.
    """
    ticker = questionary.text(
        f"Enter ticker symbol (e.g. {TICKER_INPUT_EXAMPLES}):",
        validate=lambda x: (
            is_valid_ticker_input(x)
            or "Please enter a valid ticker symbol, e.g. AAPL, 000404.SZ, 0700.HK, GC=F."
        ),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if ticker is None:
        console.print("\n[red]No ticker symbol provided. Exiting...[/red]")
        exit(1)

    return normalize_ticker_symbol(ticker) if ticker.strip() else "SPY"


def parse_ticker(value: str, exchange: str | None = None) -> str:
    """A ticker given on the command line, canonical; empty or malformed is refused.

    With ``exchange`` (``NSE``/``BSE``), a bare symbol is read as that exchange's
    listing: ``RELIANCE`` is ``RELIANCE.NS``.
    """
    from tradingagents.dataflows.symbols import resolve_symbol

    if not value.strip() or not is_valid_ticker_input(value):
        raise ValueError(f"not a ticker symbol: {value!r} (e.g. {TICKER_INPUT_EXAMPLES})")
    return resolve_symbol(value, exchange)


def parse_vendors(values: list[str] | None) -> dict[str, str]:
    """``["core_stock_apis=alpha_vantage"]`` -> ``{"core_stock_apis": "alpha_vantage"}``, each one checked.

    A category must exist and every vendor in its chain (``yfinance,alpha_vantage``)
    must serve at least one of the category's methods, so a typo stops the run
    before any model is called.
    """
    from tradingagents.dataflows.router import TOOLS_CATEGORIES, VENDOR_METHODS

    chosen: dict[str, str] = {}
    for value in values or []:
        category, sep, chain = (part.strip() for part in value.partition("="))
        if not sep or not category or not chain:
            raise ValueError(f"--vendor takes category=vendor, got {value!r}")
        if category not in TOOLS_CATEGORIES:
            raise ValueError(f"unknown data category {category!r}; choose from {', '.join(TOOLS_CATEGORIES)}")
        served = {v for method in TOOLS_CATEGORIES[category]["tools"] for v in VENDOR_METHODS.get(method, {})}
        unknown = [v for v in (v.strip() for v in chain.split(",")) if v and v not in served]
        if unknown:
            raise ValueError(f"{category} has no vendor {', '.join(unknown)}; choose from {', '.join(sorted(served))}")
        chosen[category] = chain
    return chosen


def parse_analysis_date(value: str) -> str:
    """An analysis date as YYYY-MM-DD, today or earlier."""
    try:
        day = datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"not a date: {value!r}; use YYYY-MM-DD") from None
    if day > datetime.date.today():
        raise ValueError(f"{value} is in the future")
    return day.isoformat()


def parse_horizon(value: str | None) -> str | None:
    """--horizon's value, swing or intraday in any case; None when not given."""
    from tradingagents.default_config import DECISION_HORIZONS

    if value is None:
        return None
    horizon = value.strip().lower()
    if horizon not in DECISION_HORIZONS:
        raise ValueError(f"--horizon takes {' or '.join(DECISION_HORIZONS)}, got {value!r}")
    return horizon


def parse_analysts(value: str, asset_type: AssetType) -> list[AnalystType]:
    """Comma-separated analyst names, in the canonical order, checked against the asset."""
    # "sentiment" is the name users see; the analyst's key is "social".
    aliases = {"sentiment": "social", "day-trader": "day_trader", "daytrader": "day_trader"}
    names = [aliases.get(n, n) for n in (n.strip().lower() for n in value.split(",")) if n]
    if not names:
        raise ValueError("name at least one analyst")
    known = {a.value: a for a in AnalystType}
    unknown = [n for n in names if n not in known]
    if unknown:
        choices = ", ".join("sentiment" if name == "social" else name for name in known)
        raise ValueError(f"unknown analyst {', '.join(unknown)}; choose from {choices}")
    available = filter_analysts_for_asset_type(list(known.values()), asset_type)
    unavailable = [n for n in names if known[n] not in available]
    if unavailable:
        raise ValueError(f"{', '.join(unavailable)} is not available for {asset_type.value}")
    return [a for a in available if a.value in names]


def normalize_ticker_symbol(ticker: str) -> str:
    """Resolve user input to its canonical Yahoo symbol (single source of truth).

    Delegates to the data layer's ``normalize_symbol`` so the symbol the CLI
    passes through the pipeline is exactly the one the data path will price
    (e.g. ``BTCUSD`` -> ``BTC-USD``, ``XAUUSD`` -> ``GC=F``). Falls back to the
    plain upper-case if the data layer is unavailable.
    """
    try:
        from tradingagents.dataflows.symbols import normalize_symbol

        return normalize_symbol(ticker)
    except Exception:
        return ticker.strip().upper()


def detect_asset_type(ticker: str) -> AssetType:
    """Classify on the canonical symbol so e.g. BTCUSD and BTC-USDT both read as
    crypto (#981/#982), matching what the data path will actually fetch."""
    canonical = normalize_ticker_symbol(ticker)
    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    return AssetType.STOCK


def filter_analysts_for_asset_type(
    analysts: list[AnalystType], asset_type: AssetType
) -> list[AnalystType]:
    if asset_type != AssetType.CRYPTO:
        return analysts
    # A coin has no company fundamentals, and no session for an intraday plan.
    return [
        analyst
        for analyst in analysts
        if analyst not in (AnalystType.FUNDAMENTALS, AnalystType.DAY_TRADER)
    ]


def _matching_choice(options, default):
    """The option value equal to ``default``, or None to leave the menu as is."""
    return next((value for _, value in options if value == default), None)


def select_analysts(asset_type: AssetType = AssetType.STOCK, default=None) -> list[AnalystType]:
    """Select analysts using an interactive checkbox.

    ``default`` pre-checks the previous run's analysts; the prompt still shows.
    """
    available_analysts = filter_analysts_for_asset_type(
        [value for _, value in ANALYST_CHOICES],
        asset_type,
    )
    choices = questionary.checkbox(
        "Select Your [Analysts Team]:",
        choices=[
            questionary.Choice(display, value=value, checked=value.value in (default or []))
            for display, value in ANALYST_CHOICES
            if value in available_analysts
        ],
        instruction="\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done",
        validate=lambda x: len(x) > 0 or "You must select at least one analyst.",
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print("\n[red]No analysts selected. Exiting...[/red]")
        exit(1)

    return choices


def select_research_depth(default=None) -> int:
    """Select research depth using an interactive selection."""

    DEPTH_OPTIONS = [
        ("Shallow - Quick research, few debate and strategy discussion rounds", 1),
        ("Medium - Middle ground, moderate debate rounds and strategy discussion", 3),
        ("Deep - Comprehensive research, in depth debate and strategy discussion", 5),
    ]

    choice = questionary.select(
        "Select Your [Research Depth]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        default=_matching_choice(DEPTH_OPTIONS, default),
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No research depth selected. Exiting...[/red]")
        exit(1)

    return choice


# Mainstream OpenRouter chat-LLM provider namespaces. We surface the newest
# models from these rather than the universal-newest, which is dominated by
# niche/experimental releases. These are the general-purpose chat providers;
# more enterprise/specialised namespaces (nvidia, cohere, amazon, ...) tend to
# ship research/safety variants as their newest, so they're left out of the
# shortlist. Provider names are stable (unlike model IDs), so this rarely needs
# touching; anything not here is still reachable via Custom ID.
_OPENROUTER_MAINSTREAM = {
    "openai", "anthropic", "google", "deepseek", "qwen", "mistralai",
    "meta-llama", "x-ai", "z-ai", "minimax", "moonshotai",
}


def _fetch_openrouter_models() -> list[tuple[str, str]]:
    """Fetch available models from the OpenRouter API."""
    import requests
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        # Newest first so the top-N shown really is the latest available — the
        # API currently returns this order, but sort explicitly so the prompt's
        # "latest available" label holds regardless of response ordering.
        models.sort(key=lambda m: m.get("created") or 0, reverse=True)
        return [(m.get("name") or m["id"], m["id"]) for m in models]
    except Exception as e:
        console.print(f"\n[yellow]Could not fetch OpenRouter models: {e}[/yellow]")
        return []


def select_chatgpt_model(mode: str, default=None) -> str:
    """Select one of the ChatGPT plan's models, or enter a model ID."""
    from tradingagents.llm_clients import chatgpt_auth
    try:
        models = chatgpt_auth.list_models()
    except Exception as e:
        console.print(f"\n[yellow]Could not list your ChatGPT plan's models: {escape(str(e))}[/yellow]")
        models = []
    return _select_listed_model(f"Select Your [{mode.title()}-Thinking] ChatGPT Plan Model:",
                                models, default, _prompt_custom_model_id)


def _select_listed_model(title: str, models, default, custom) -> str:
    """Select from ``models`` ((name, model ID) pairs, listed live) or enter one with ``custom``."""
    choices = [questionary.Choice(name, value=mid) for name, mid in models]
    choices.append(questionary.Choice("Custom model ID", value="custom"))
    choice = questionary.select(
        title,
        choices=choices,
        default=default if any(c.value == default for c in choices) else None,
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=_MENU_STYLE,
    ).ask()
    if choice is None:
        console.print("\n[red]No model selected. Exiting...[/red]")
        exit(1)
    return custom() if choice == "custom" else choice


def _require_text(message: str, hint: str, valid=bool) -> str:
    """Prompt for a required value; exit cleanly if the user cancels.

    ``questionary.text(...).ask()`` returns None on Ctrl-C/Esc; mirror the
    exit-on-cancel behavior of the other required selections so a cancelled
    prompt never returns an empty model/deployment that would fail downstream.
    ``valid`` checks the stripped answer (by default, that it is not empty);
    ``hint`` is shown until it passes.
    """
    response = questionary.text(
        message,
        validate=lambda x: valid(x.strip()) or hint,
    ).ask()
    if response is None:
        console.print("\n[red]Cancelled. Exiting...[/red]")
        exit(1)
    return response.strip()


def select_openrouter_model(mode: str, default=None) -> str:
    """Select an OpenRouter model from the newest available, or enter a custom ID.

    ``mode`` ("quick"/"deep") labels the prompt so the two consecutive
    OpenRouter selections are distinguishable, like the other providers (#1000).
    """
    models = _fetch_openrouter_models()  # newest first
    # Prefer the newest from mainstream providers so the shortlist isn't crowded
    # out by niche/experimental releases; fall back to all if none match.
    mainstream = [
        (name, mid) for name, mid in models
        if not mid.startswith("~")  # skip variant/alias duplicate routes
        and mid.split("/", 1)[0] in _OPENROUTER_MAINSTREAM
    ]
    top = (mainstream or models)[:5]
    return _select_listed_model(
        f"Select Your [{mode.title()}-Thinking] OpenRouter Model (latest available):", top, default,
        lambda: _require_text("Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):",
                              "Please enter a model ID."))


# Providers whose models are listed live, with the menu that offers them.
LISTED_LIVE = {"openrouter": select_openrouter_model, "chatgpt": select_chatgpt_model}


def _prompt_custom_model_id(provider: str | None = None) -> str:
    """Prompt user to type a custom model ID; a Claude Code one is checked as it is typed."""
    if provider == "claude_code":
        from tradingagents.llm_clients.claude_code_client import MODEL_EXAMPLES, is_model_id

        return _require_text(f"Enter model ID (such as {MODEL_EXAMPLES}):",
                             "A model ID has no spaces, e.g. claude-opus-5-5 or opus.", is_model_id)
    return _require_text("Enter model ID:", "Please enter a model ID.")


def _select_model(provider: str, mode: str, default=None) -> str:
    """Select a model for the given provider and mode (quick/deep)."""
    listed = LISTED_LIVE.get(provider.lower())
    if listed is not None:
        return listed(mode, default)

    if provider.lower() == "azure":
        return _require_text(
            f"Enter Azure deployment name ({mode}-thinking):",
            "Please enter a deployment name.",
        )

    choice = questionary.select(
        f"Select Your [{mode.title()}-Thinking LLM Engine]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in get_model_options(provider, mode)
        ],
        default=_matching_choice(get_model_options(provider, mode), default),
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=_MENU_STYLE,
    ).ask()

    if choice is None:
        console.print(f"\n[red]No {mode} thinking llm engine selected. Exiting...[/red]")
        exit(1)

    if choice == "custom":
        return _prompt_custom_model_id(provider.lower())

    return choice


def select_shallow_thinking_agent(provider, default=None) -> str:
    """Select shallow thinking llm engine using an interactive selection."""
    return _select_model(provider, "quick", default)


def select_deep_thinking_agent(provider, default=None) -> str:
    """Select deep thinking llm engine using an interactive selection."""
    return _select_model(provider, "deep", default)


def _llm_provider_table() -> list[tuple[str, str, str | None]]:
    """(display_name, provider_key, base_url) for every supported provider.

    Shared by the interactive picker and by env-driven configuration so an
    env-set provider resolves to the same default endpoint the menu uses.
    Ollama users can point at a remote ollama-serve via OLLAMA_BASE_URL
    (convention from the broader Ollama ecosystem); falls back to the
    localhost default when unset.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
    return [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Google", "google", None),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        ("ChatGPT (Plus or Pro plan)", "chatgpt", "https://api.openai.com/v1"),
        ("Claude (Pro or Max plan, via Claude Code)", "claude_code", None),
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen", "qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        # Z.AI international, the platform ZHIPU_API_KEY belongs to; the CN
        # platform is the separate glm-cn key, chosen in the region prompt.
        ("GLM", "glm", "https://api.z.ai/api/paas/v4/"),
        ("MiniMax", "minimax", "https://api.minimax.io/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Mistral", "mistral", "https://api.mistral.ai/v1"),
        ("Kimi (Moonshot)", "kimi", "https://api.moonshot.ai/v1"),
        ("Groq", "groq", "https://api.groq.com/openai/v1"),
        ("NVIDIA NIM", "nvidia", "https://integrate.api.nvidia.com/v1"),
        ("Azure OpenAI", "azure", None),
        ("Amazon Bedrock", "bedrock", None),
        ("Ollama", "ollama", ollama_url),
        ("OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)", "openai_compatible", None),
    ]


def provider_default_url(provider_key: str) -> str | None:
    """Return the default backend URL for a provider key, or None if unknown."""
    key = provider_key.lower()
    for _, pk, url in _llm_provider_table():
        if pk == key:
            return url
    return None


def resolve_backend_url(
    provider: str, menu_url: str | None = None, env_url: str | None = None
) -> str | None:
    """Resolve the backend URL with the correct precedence.

    An explicit env override (``env_url``, from ``TRADINGAGENTS_LLM_BACKEND_URL``
    via ``DEFAULT_CONFIG['backend_url']``) is honored regardless of how the
    provider was chosen — interactively or from the environment (#978).
    Otherwise the menu/region URL, then the provider's default.
    """
    return env_url or menu_url or provider_default_url(provider)


# A URL's user name and password: what comes before an @ in its host part.
_USERINFO = re.compile(r"^((?:[a-z][a-z0-9+.-]*:)?//)?[^/?#]*@", re.IGNORECASE)


def shown_url(url: str | None) -> str | None:
    """``url`` as printed, its user name, password and query as ``***``: they can hold
    credentials, and the output can end up in logs. The run still uses ``url`` as given."""
    if not url:
        return url
    shown, query, _ = _USERINFO.sub(r"\1***@", url).partition("?")
    return f"{shown}?***" if query else shown


def prompt_openai_compatible_url(default=None) -> str:
    """Prompt for a custom OpenAI-compatible endpoint base URL."""
    url = questionary.text(
        "Enter the OpenAI-compatible base URL "
        "(e.g. http://localhost:8000/v1 for vLLM, http://localhost:1234/v1 for LM Studio):",
        default=default or "",
        validate=lambda x: x.strip().startswith(("http://", "https://"))
        or "Enter a URL starting with http:// or https://",
    ).ask()
    if not url:
        console.print("\n[red]No endpoint URL provided. Exiting...[/red]")
        exit(1)
    return url.strip()


# The menu entry that groups the providers used through a personal plan.
_SUBSCRIPTION = ("subscription", None)
# The plans under it: ChatGPT signs in here (`tradingagents login chatgpt`), a
# Claude plan in the person's own Claude Code.
SUBSCRIPTION_PROVIDERS = (*SIGNED_IN_PROVIDERS, "claude_code")


def select_llm_provider(default=None) -> tuple[str, str | None]:
    """Select the LLM provider and its API endpoint."""
    PROVIDERS = _llm_provider_table()
    # A region-specific key (qwen-cn) is chosen in a later prompt; the menu
    # lists the base provider.
    base = (default or "").split("-cn")[0]
    choices = []
    for display, provider_key, url in PROVIDERS:
        if provider_key in SUBSCRIPTION_PROVIDERS:
            continue
        choices.append(questionary.Choice(display, value=(provider_key, url)))
        if provider_key == "anthropic":
            choices.append(questionary.Choice("Subscription (use your ChatGPT or Claude plan)", value=_SUBSCRIPTION))
    preselected = _SUBSCRIPTION if base in SUBSCRIPTION_PROVIDERS else next(
        ((key, url) for _, key, url in PROVIDERS if key == base), None
    )

    choice = questionary.select(
        "Select your LLM Provider:",
        choices=choices,
        default=preselected,
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=_MENU_STYLE,
    ).ask()

    if choice is None:
        console.print("\n[red]No LLM provider selected. Exiting...[/red]")
        exit(1)

    if choice == _SUBSCRIPTION:
        choice = questionary.select(
            "Select your subscription:",
            choices=[questionary.Choice(display, value=(key, url))
                     for display, key, url in PROVIDERS if key in SUBSCRIPTION_PROVIDERS],
            style=_MENU_STYLE,
        ).ask()
        if choice is None:
            console.print("\n[red]No subscription selected. Exiting...[/red]")
            exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """Ask for OpenAI reasoning effort level."""
    choices = [
        questionary.Choice("Medium (Default)", "medium"),
        questionary.Choice("High (More thorough)", "high"),
        questionary.Choice("Low (Faster)", "low"),
    ]
    return questionary.select(
        "Select Reasoning Effort:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_anthropic_effort() -> str | None:
    """Ask for Anthropic effort level.

    Controls token usage and response thoroughness on Claude 4.5 / 4.6 / 4.7
    models. The API also accepts "max"; we expose low/medium/high as the
    common selection range.
    """
    return questionary.select(
        "Select Effort Level:",
        choices=[
        questionary.Choice("High (recommended)", "high"),
        questionary.Choice("Medium (balanced)", "medium"),
        questionary.Choice("Low (faster, cheaper)", "low"),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_gemini_thinking_config() -> str | None:
    """Ask for Gemini thinking configuration.

    Returns thinking_level: "high" or "minimal".
    Client maps to appropriate API param based on model series.
    """
    return questionary.select(
        "Select Thinking Mode:",
        choices=[
        questionary.Choice("Enable Thinking (recommended)", "high"),
        questionary.Choice("Minimal/Disable Thinking", "minimal"),
        ],
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
    ).ask()


def ask_glm_region() -> tuple[str, str]:
    """Ask which GLM platform (Z.AI international vs BigModel China) to use.

    Zhipu serves the same GLM models under two brands with separate
    accounts; keys aren't interchangeable. Returns (provider_key, backend_url).
    """
    return questionary.select(
        "Select GLM platform:",
        choices=[
            questionary.Choice(
                "Z.AI — api.z.ai (international, uses ZHIPU_API_KEY)",
                value=("glm", "https://api.z.ai/api/paas/v4/"),
            ),
            questionary.Choice(
                "BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)",
                value=("glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_qwen_region() -> tuple[str, str]:
    """Ask which Qwen region (international vs China) to use.

    Alibaba DashScope exposes two endpoints with separate accounts —
    a key from one region does NOT authenticate against the other
    (fixes #758). Returns (provider_key, backend_url).
    """
    return questionary.select(
        "Select Qwen region:",
        choices=[
            questionary.Choice(
                "International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)",
                value=("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            ),
            questionary.Choice(
                "China — dashscope.aliyuncs.com (uses DASHSCOPE_CN_API_KEY)",
                value=("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_minimax_region() -> tuple[str, str]:
    """Ask which MiniMax region (global vs China) to use.

    MiniMax exposes two endpoints with separate accounts — a key from
    one region does NOT authenticate against the other. Returns
    (provider_key, backend_url).
    """
    return questionary.select(
        "Select MiniMax region:",
        choices=[
            questionary.Choice(
                "Global — api.minimax.io (uses MINIMAX_API_KEY)",
                value=("minimax", "https://api.minimax.io/v1"),
            ),
            questionary.Choice(
                "China — api.minimaxi.com (uses MINIMAX_CN_API_KEY)",
                value=("minimax-cn", "https://api.minimaxi.com/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def confirm_ollama_endpoint(url: str) -> None:
    """Show the resolved Ollama endpoint after provider selection.

    Surfaces three things the user benefits from seeing before model
    selection: which URL we'll actually hit, where it came from
    (`OLLAMA_BASE_URL` vs default), and a soft warning if the URL is
    missing the scheme/port that ollama-serve expects. The warning is
    advisory only — we don't reject malformed input, since the user may
    be doing something deliberately unusual (e.g. a reverse-proxy path).
    """
    from_env = os.environ.get("OLLAMA_BASE_URL")
    origin = " (from OLLAMA_BASE_URL)" if from_env and from_env == url else ""
    shown = shown_url(url)
    console.print(f"[green]✓ Using Ollama at {shown}{origin}[/green]")

    if not url.startswith(("http://", "https://")):
        console.print(
            f"[yellow]Note: {shown!r} is missing a scheme. "
            f"Ollama-serve typically expects a URL like "
            f"http://<host>:11434/v1.[/yellow]"
        )
    elif ":11434" not in url and "://localhost" not in url and "://127.0.0.1" not in url:
        # Soft hint when the port differs from the ollama-serve default
        # and the host isn't local (where users sometimes proxy on :80).
        console.print(
            f"[yellow]Note: {shown!r} doesn't include port 11434. "
            f"Make sure your remote ollama-serve listens on the port "
            f"shown above.[/yellow]"
        )


def ensure_signed_in(provider: str) -> None:
    """Make sure a plan provider's sign-in works before the run, offering to sign in now.

    Signing in runs as its own process (``tradingagents login chatgpt``), so its
    read of a pasted address ends with it and never takes a later answer. A Claude
    plan is signed in to in Claude Code itself, so here it is only checked."""
    if provider.lower() == "claude_code":
        from tradingagents.llm_clients import claude_code_client

        try:
            plan = claude_code_client.signed_in_plan()
        except claude_code_client.ClaudeCodeError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[green]✓ Using your Claude plan through Claude Code:[/green] {escape(plan)}")
        return
    if provider.lower() not in SIGNED_IN_PROVIDERS:
        return
    from tradingagents.llm_clients import chatgpt_auth  # the one signed-in provider

    try:
        chatgpt_auth.access_token()
    except chatgpt_auth.ChatGPTUnreachable as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    except chatgpt_auth.ChatGPTAuthError as exc:
        if not sys.stdin.isatty():
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"\n[yellow]{escape(str(exc))}[/yellow]")
        # -P keeps a cli/ package in the working directory from shadowing ours.
        login = [sys.executable, "-P", "-m", "cli.main", "login", "chatgpt"]
        if subprocess.run(login).returncode != 0:
            raise typer.Exit(code=1) from None
    console.print(f"[green]✓ Using your ChatGPT plan:[/green] {escape(chatgpt_auth.signed_in_as() or '')}")


def ensure_api_key(provider: str) -> str | None:
    """Make sure the API key for `provider` is available in the environment.

    If the env var is already set, returns its value untouched. Otherwise
    interactively prompts the user, persists the value to the project's
    .env file via python-dotenv's set_key (creating .env if needed), and
    exports it into os.environ so the current process picks it up.

    Returns None for providers that do not require a key (e.g. ollama)
    and for providers not found in the canonical mapping.
    """
    env_var = get_api_key_env(provider)
    if env_var is None:
        return None  # ollama / unknown — no key check possible

    # Key-optional providers (generic OpenAI-compatible / local servers) read the
    # key when present but must never force an interactive prompt.
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider.lower())
    if spec is not None and spec.key_optional:
        return os.environ.get(env_var)

    existing = os.environ.get(env_var)
    if existing:
        return existing

    if not sys.stdin.isatty():
        console.print(f"[red]{env_var} is not set; there is no terminal to ask for it.[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"\n[yellow]{env_var} is not set in your environment.[/yellow]"
    )
    key = questionary.password(
        f"Paste your {env_var} (will be saved to .env):",
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
    ).ask()
    if not key:
        console.print(
            f"[red]Skipped. API calls will fail until {env_var} is set.[/red]"
        )
        return None

    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    # The file holds credentials, so make it owner-only before writing: create
    # it 0600 when absent, and tighten an existing one (set_key keeps the mode).
    if not os.path.exists(env_path):
        os.close(os.open(env_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    os.chmod(env_path, 0o600)
    set_key(env_path, env_var, key)
    os.environ[env_var] = key
    console.print(f"[green]Saved {env_var} to {env_path}[/green]")
    return key


def ask_output_language(default=None) -> str:
    """Ask for report output language.

    ``default`` is offered only when it is one of the listed languages: a custom
    one entered last time is free text, which the menu cannot preselect.
    """
    choices = [
        questionary.Choice("English (default)", "English"),
        questionary.Choice("Chinese (中文)", "Chinese"),
        questionary.Choice("Japanese (日本語)", "Japanese"),
        questionary.Choice("Korean (한국어)", "Korean"),
        questionary.Choice("Hindi (हिन्दी)", "Hindi"),
        questionary.Choice("Spanish (Español)", "Spanish"),
        questionary.Choice("Portuguese (Português)", "Portuguese"),
        questionary.Choice("French (Français)", "French"),
        questionary.Choice("German (Deutsch)", "German"),
        questionary.Choice("Arabic (العربية)", "Arabic"),
        questionary.Choice("Russian (Русский)", "Russian"),
        questionary.Choice("Custom language", "custom"),
    ]
    choice = questionary.select(
        "Select Output Language:",
        choices=choices,
        default=_matching_choice([(c.title, c.value) for c in choices], default),
        style=questionary.Style([
            ("selected", "fg:yellow noinherit"),
            ("highlighted", "fg:yellow noinherit"),
            ("pointer", "fg:yellow noinherit"),
        ]),
    ).ask()

    # Output language has a sensible default, so a cancel falls back to English
    # rather than exiting the run (unlike the required model/provider prompts).
    if choice is None:
        return "English"
    if choice == "custom":
        return (questionary.text(
            "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):",
            validate=lambda x: len(x.strip()) > 0 or "Please enter a language name.",
        ).ask() or "").strip() or "English"

    return choice
