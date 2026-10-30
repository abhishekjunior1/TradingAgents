<p align="center">
  <img src="tradingagents/assets/tauric-logo.svg" width="60%" alt="Tauric Research">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>
<br>
<div align="center">
  <a href="https://github.com/TauricResearch" target="_blank"><img alt="TradingAgents #1 Repository of the Day" src="https://trendshift.io/api/badge/repositories/16192" width="250" height="55"/></a>
</div>
<br>
<div align="center">
  <!-- These translate the upstream TauricResearch/TradingAgents README, not this fork's. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: Multi-Agents LLM Financial Trading Framework

> **This repository is an independent fork** of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), based on its v0.7.1 release, with the additions listed in [What this fork adds](#what-this-fork-adds). It is not an official Tauric Research release. The framework, the logo, badges and community links above, and the translations they offer (of the upstream README, without this fork's additions) are Tauric Research's; the [paper](https://arxiv.org/abs/2412.20138) is by Yijia Xiao, Edward Sun, Di Luo and Wei Wang.

## What this fork adds

- **Indian markets (NSE/BSE).** Tickers as brokers write them (`NSE:RELIANCE`, `RELIANCE-EQ`) and index names (`NIFTY 50`, `SENSEX`); IST sessions with the 2026 NSE/BSE holidays; market context (NIFTY 50, SENSEX, India VIX, the closest NIFTY sector index, USD/INR, Brent); INR portfolios with configurable charges and capital-gains tax; and the `market` command with watchlists. [More](#indian-markets-nse--bse) · [docs/india.md](docs/india.md)
- **Intraday (day-trader) plans.** `--horizon intraday` adds a Day Trader analyst that reads 5-minute bars, and the Trader and Portfolio Manager write a paper plan for one session (direction, entry, stop, target, exit-by time, size), which a later run scores on that session's bars. [More](#intraday-day-trader-plans) · [docs/intraday.md](docs/intraday.md)
- **The `/day-trader` skill for Claude Code.** `/day-trader RELIANCE` (or `/day-trader TCS bse`) runs that analysis for one NSE or BSE stock and summarises the plan from the saved report. [More](docs/intraday.md#the-day-trader-skill-claude-code)
- **Scenarios in every decision.** For any market, the Portfolio Manager's decision also gives bull, base and bear cases, key risks and a confidence level.
- **Risk figures for your portfolio.** With `--portfolio`, the trader, risk analysts and Portfolio Manager also get each holding's volatility, drawdown, beta and return; for a book in one currency, its concentration and sector exposure; and a sizing reference when the book is in the currency the analyzed ticker trades in, which is known here for NSE/BSE listings (INR), US listings (USD) and crypto pairs (the currency or coin each is quoted in: BTC-INR in INR, ETH-BTC in BTC). [More](#current-holdings)
- **Your Claude plan.** The `claude_code` provider runs the agents' model calls on your Claude Pro or Max plan through your own Claude Code sign-in; a model tier set to another provider, and Jev screening with `TYPESAFE_API_KEY`, keep their own keys. [More](#required-apis)
- **Web research through Agent-Reach (optional).** Web search, web pages and YouTube coverage as news sources, with no account or key. [More](#web-research-through-agent-reach-optional) · [docs/agent-reach.md](docs/agent-reach.md)
- **Paper trading on AlphArena (optional).** A swing decision on a US stock or ETF can be sent to AlphArena as a paper trade with simulated money, after you confirm it or pass `--arena-yes`. [More](#paper-trading-on-alpharena-optional) · [docs/arena.md](docs/arena.md)

```bash
tradingagents --exchange nse --ticker RELIANCE --analysts market,news,fundamentals        # rate an NSE stock
tradingagents --horizon intraday --exchange nse --ticker TCS --analysts market,news       # a paper plan for one session
tradingagents market                                                                       # NSE session, indices, India VIX, USD/INR, Brent; no model called
```

Like the original, this fork is for research and paper trading only: it places no real orders, has no broker integration and reads no brokerage credentials, and its output is not investment advice, nor advice from a SEBI-registered research analyst or investment adviser. None of these additions was part of the paper's experiments (daily decisions on US technology stocks from January to March 2024), so its reported results say nothing about them, and the rest of the code has changed since those experiments too (see [Reproducibility](#reproducibility)). Every change is listed under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).

## News

<!-- news:start -->
- [2026-10] **This fork** adds Indian markets (NSE/BSE), intraday day-trader paper plans with a `/day-trader` skill for Claude Code, a `claude_code` provider for Claude Pro or Max plans, and optional Agent-Reach web research and AlphArena paper trading, on top of v0.7.1; see [What this fork adds](#what-this-fork-adds).
- [2026-10] **TradingAgents v0.7.1** released with decisions scored only on final closes and settled across long market holidays, a fix for model dates that could read filings made after the run date, and fixes to portfolios, prices, SEC statements and reports.
- [2026-10] **TradingAgents v0.7.0** released with runs on your own ChatGPT Plus or Pro plan, backtests scored from the first close after each decision, a note at start-up when a newer release is out, and a wheel attached to each GitHub release.
- [2026-10] **TradingAgents v0.6.0** released with reports saved as one HTML page, a provider per model tier so the managers and analysts can run on different models, past decisions settled for every ticker while the analysts work, and company news read from Yahoo search while Yahoo's news feed is down.

Full release notes are in [CHANGELOG.md](CHANGELOG.md).

<details>
<summary>Earlier news</summary>

- [2026-09] **TradingAgents v0.5.2** released with parallel analysts for a faster analysis, a CLI that runs without prompts from flags such as `--ticker` and `--date`, the run's settings recorded in every report, and backtests that see only data published by each analysis date.
- [2026-09] **TradingAgents v0.5.1** released with a package layout organised by what each module holds (import paths moved), optional Jev screening of social posts, GPT-6 Sol and Luna as the default models, and fixes to run isolation and SEC EDGAR statements.
- [2026-09] **TradingAgents v0.5.0** released with point-in-time integrity across every dated path, SEC EDGAR fundamentals served as filed, backtesting over a ticker and date grid, portfolio-aware runs, and current model lineups across every provider.
- [2026-08] **TradingAgents v0.4.0** released with look-ahead / point-in-time fixes across FRED macro, social sentiment, and the decision-log memory; clearer decision signals; working CLI checkpoint resume; Trader price grounding; and the GPT-5.6 and GLM-5.3 models.
- [2026-07] **TradingAgents v0.3.1** released with correctness and stability fixes: Alpha Vantage look-ahead filtering, graph-router crash-safety, graph-shape-aware checkpoint resume, working crypto sentiment sources, a configurable LLM retry budget, Bedrock API-key auth, and Claude Sonnet 5 / Fable 5 support.
- [2026-06] **TradingAgents v0.3.0** released with a verified data-access contract, an expanded provider registry (NVIDIA, Kimi, Groq, Mistral, Bedrock, and any OpenAI-compatible endpoint), FRED and Polymarket data vendors, a current-generation model catalog, and a CI gate.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

</details>
<!-- news:end -->

<div align="center">

✨ [What this fork adds](#what-this-fork-adds) | 🚀 [TradingAgents](#tradingagents-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

> 🎉 Tauric Research officially released **TradingAgents** and fully open-sourced the framework, thanking its community for the enthusiasm and inquiries about the work.

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>
<p align="center"><em>Figure 1 of the paper, with the data sources and the OpenAI o1 model its experiments used. This code has no Bloomberg, Reuters, X, EODHD or FinnHub feed, and its default models differ (see <a href="#required-apis">Required APIs</a>).</em></p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

TradingAgents decomposes complex trading tasks into specialized roles.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.
- Day Trader (added by this fork; intraday mode only): reads 5-minute bars for one session's levels (opening range, VWAP, pivots, ATR) and paper position sizes, so the team can plan that session ([intraday plans](#intraday-day-trader-plans)).

The selected analysts work at the same time, each on its own tools, and the research debate starts once all of their reports are in.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager makes the final call: a rating from Buy to Sell with its thesis and, added by this fork, bull, base and bear cases, key risks and a confidence level; in intraday mode the decision also carries a paper plan for one session. In the paper's backtests the approved trade was executed in a simulation; this code sends nothing to a broker, and only a CLI run with [AlphArena](#paper-trading-on-alpharena-optional) switched on can send a swing decision on a US stock or ETF dated today in New York, as a paper trade, after you confirm it or pass `--arena-yes`.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone this fork (the upstream repository and Tauric Research's release downloads do not include its additions):
```bash
git clone https://github.com/abhishekjunior1/TradingAgents.git
cd TradingAgents
```

TradingAgents needs Python 3.11 or later. Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

Or with [uv](https://docs.astral.sh/uv/):
```bash
uv venv --python 3.13
source .venv/bin/activate
```

Install the package and its dependencies (`uv pip install .` with uv):
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

After updating the repository, rebuild the image with `docker compose build`.

Results, reports, the memory log and the cache live in the `tradingagents_data` volume. To keep them in a folder on the host instead, create the folder and point `TRADINGAGENTS_DATA_DIR` at it, in `.env` or the shell: `mkdir -p data && TRADINGAGENTS_DATA_DIR=./data docker compose run --rm tradingagents`.

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen (international, dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen (China, dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax (global, api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax (China, api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export MISTRAL_API_KEY=...         # Mistral
export MOONSHOT_API_KEY=...        # Kimi (Moonshot)
export GROQ_API_KEY=...            # Groq
export NVIDIA_API_KEY=...          # NVIDIA NIM
export FRED_API_KEY=...            # FRED macro data (free, optional)
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
export TYPESAFE_API_KEY=...        # Jev social-post screening (optional)
export ALPHARENA_API_KEY=...       # AlphArena paper trading (optional; its sandbox aa_test_ key first)
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials. With Docker, put these keys in `.env` instead: the image does not include `.env` files.

To run on your ChatGPT Plus or Pro plan instead of an API key, choose "Subscription" in the CLI's provider menu, or set `llm_provider: "chatgpt"`. Sign in once with `tradingagents login chatgpt`: it prints an address to open in your browser, which returns to this machine on `127.0.0.1`. When the browser runs on another machine, or TradingAgents runs in Docker (`docker compose run --rm tradingagents login chatgpt`), paste the address the browser ends up on. The sign-in is kept in `~/.tradingagents/chatgpt-auth.json`, and `tradingagents logout chatgpt` revokes it. Runs count toward your plan's usage limits, and the plan does not accept `temperature` or `max_tokens`.

To run on your Claude Pro or Max plan instead of an API key, install [Claude Code](https://code.claude.com) and sign in with `claude auth login` using your Claude account, then choose "Subscription" and then "Claude" in the CLI's provider menu, or set `llm_provider: "claude_code"`. Anthropic allows a plan only in the unmodified Claude Code app, so TradingAgents never sees your sign-in: each model call runs your own `claude -p`, started with no tools, MCP servers, hooks, plugins, settings or CLAUDE.md, saving nothing, in an empty folder, and the analysts' data tools stay in TradingAgents. `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` are withheld from those calls, so they use the plan rather than a key, and so are your other keys and tokens (variables named like `*_API_KEY`, `*_TOKEN`, `*_SECRET` or `*_PASSWORD`), which Claude Code does not need. Runs count toward your plan's usage limits, which Anthropic sizes for ordinary individual use; a full run makes dozens of model calls, each taking a few seconds longer than a direct API call. Claude Code takes no `temperature` or `max_tokens`, and the Docker image does not include it.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set `llm_provider: "bedrock"`, configure AWS credentials (environment variables, `~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model ID, e.g. `us.anthropic.claude-opus-5-5`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay), use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires one.

With `TYPESAFE_API_KEY` set, the Sentiment Analyst screens StockTwits and Reddit posts with TypeSafe's Jev before reading them. Posts that are not about the company are dropped, and each source opens with a count of the remaining posts by stance: bullish, bearish, neutral, or unclear. Without the key, posts pass through unscreened. `jev-latest` moves with new releases; set `TYPESAFE_DEFAULT_MODEL` to a versioned ID such as `jev-1.13.0` to hold it fixed across runs. To reach Jev through OpenRouter, put an OpenRouter key in `TYPESAFE_API_KEY` and set `TYPESAFE_BASE_URL=https://openrouter.ai/api`.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more. Your previous run's answers come back as the defaults, so pressing Enter accepts them; a run without a terminal (a scheduled job, the `/day-trader` skill) leaves them as they were. The `TRADINGAGENTS_*` variables in `.env` still skip their step entirely.

To run without questions, for a scheduled job or a script, answer the per-run steps with flags and the rest with `TRADINGAGENTS_*` variables:
```bash
export TRADINGAGENTS_LLM_PROVIDER=openai TRADINGAGENTS_QUICK_THINK_LLM=gpt-6-luna TRADINGAGENTS_DEEP_THINK_LLM=gpt-6-sol
export TRADINGAGENTS_OUTPUT_LANGUAGE=English TRADINGAGENTS_MAX_DEBATE_ROUNDS=1 TRADINGAGENTS_MAX_RISK_ROUNDS=1
tradingagents --ticker NVDA --date 2026-09-23 --analysts market,news,fundamentals --save --no-show
```
Each flag skips only its own question. Run without a terminal, a missing answer stops the run before it starts and names the flag or variable to set. The built-in models are OpenAI's: on `openai`, setting one of `TRADINGAGENTS_QUICK_THINK_LLM` and `TRADINGAGENTS_DEEP_THINK_LLM` leaves the other tier on its built-in model, while on any other provider a tier without its variable has no default, so a run without a terminal needs both and one at a terminal asks for the missing one.

To choose the data vendors for one run, pass `--vendor category=vendor` (repeatable; several vendors, comma-separated, make an ordered fallback), for example `--vendor news_data=yfinance,alpha_vantage`. The run stops before it starts, saying what to change, when a category or vendor is unknown or when the chain leaves one of the category's tools without a vendor that can serve it: `fundamental_data=sec_edgar` needs a fallback for the company overview (`sec_edgar,yfinance`), `news_data=agent_reach` one for insider trades (`agent_reach,yfinance`), and Alpha Vantage counts only with `ALPHA_VANTAGE_API_KEY` set.

At start-up the interactive CLI mentions a newer release and notices from Tauric Research. It checks at most once a day, in the background, and sends nothing about your runs; set `TRADINGAGENTS_NO_UPDATE_CHECK=1` to turn it off. Runs without a terminal never check. In this fork the notice still reports Tauric Research's releases, which do not include this fork's additions; the upgrade command it shows updates from the repository you installed from, so an upstream release arrives only once it has been merged into this fork.

A saved report also includes `complete_report.html`, the report as one page with its sections listed beside the text, for reading in a browser, on a phone or in print. Answering the save question at the prompt also asks about the page and can open it in your browser; `--no-html` skips it, and so does `ta.save_reports(state, "NVDA", html=False)` from Python.

### Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- India: `RELIANCE.NS`, `.BO` · Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

To measure a swing decision against another index or symbol, pass `--benchmark` (`--benchmark "NIFTY IT"`) or set `TRADINGAGENTS_BENCHMARK_TICKER` (`benchmark_ticker` in a Python config); the saved report's header names it. A swing run checks it before any model is called, and the CLI checks it at `--date`'s day before its first question, with at most one Yahoo request, whose prices the run reuses; a date typed at the prompt is checked when the run starts, and a refusal then is one line, not a traceback. An intraday plan has no benchmark, so an intraday run neither checks nor uses the value, and `--benchmark` prints a line saying it does not apply: the run's portfolio risk figures, and for a listing outside NSE/BSE the market context's Benchmark row, use the listing's index. India VIX, VIX at every term and the other volatility indices listed in `tradingagents/memory/settlement.py` (other Cboe indices, MOVE, and the Hang Seng, Nikkei 225, ASX 200 and VSTOXX ones) are refused, as is an index Yahoo keeps no daily history for (NIFTY Midcap 100, say). An Indian index Yahoo keeps a history for, or a `benchmark_map` value, is taken as it is, except that a run or backtest cell dated more than 30 days back needs a Yahoo close of it in the 30 days to that date, so a cell before the index's Yahoo history (NIFTY Financial Services' starts on 2011-09-07) is refused before it runs. Any other symbol needs a Yahoo close in the last 30 days and in the 30 days to the run's date, so a past-dated run, or a backtest cell, whose benchmark was listed after its date (a new ETF) is refused before it starts. A refusal gives the reason and the indices to choose from, and the CLI's names `--benchmark` or `TRADINGAGENTS_BENCHMARK_TICKER`, whichever set the value; a symbol that cannot be checked because Yahoo is unreachable stops the run too.

#### Indian markets (NSE / BSE)

NSE and BSE listings get Indian market data, rules and reasoning: tickers as brokers write them (`NSE:RELIANCE`, `RELIANCE-EQ`, `NSE:RELIANCE-EQ`, or `RELIANCE` with `--exchange nse`) and index names (`NIFTY 50`, `BANKNIFTY`, `SENSEX`); IST sessions, the 2026 NSE/BSE holiday calendar and the closing-auction hours; a market-context tool (NIFTY 50, SENSEX, NIFTY Bank, India VIX, the closest NIFTY sector index, USD/INR, Brent); dividends, splits and results dates; global news in India and Global sections (Yahoo carries little India news; see docs/india.md) and Indian subreddits; India macro series from FRED; INR portfolios with risk figures, configurable trading charges and capital-gains tax; and reports that say what they are (research, not SEBI-registered advice; no orders placed).

```bash
tradingagents --exchange nse --ticker RELIANCE --analysts market,news,fundamentals
tradingagents market --watchlist HDFCBANK,ICICIBANK,SBIN   # NSE session, indices, rupee, crude and these stocks; no model called
```

See [docs/india.md](docs/india.md) for setup, data sources and their limits and costs, charges and tax, and what is not available. Intraday (MIS) paper plans for NSE/BSE stocks are in [docs/intraday.md](docs/intraday.md), and the optional Agent-Reach web search is another source of India news ([docs/agent-reach.md](docs/agent-reach.md)).

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Intraday (day-trader) plans

`--horizon intraday` (or `TRADINGAGENTS_DECISION_HORIZON=intraday` for every run, which backtests refuse; `--horizon swing` overrides it for one run) plans one trading session instead of rating a position over the holding period. The Day Trader joins the analysts, and the Trader and Portfolio Manager write a paper plan for the session: direction, entry, stop, target, exit-by time, size and what invalidates it. The date defaults to today (the listing's date when it is behind your machine's: New York's for a US ticker from India after midnight), and a past `--date` plans the session after it. A later run scores the plan on that session's 5-minute bars once its close is final, with charges counted on NSE/BSE plans (configurable: `TRADINGAGENTS_INTRADAY_COST_PROFILE`, `intraday_cost_overrides`). The memory log keeps one swing and one intraday decision per ticker and date: a second intraday run on the same ticker and date is not logged, and the run warns that it will not be scored; a plan not settled while Yahoo still serves its session's 5-minute bars (about 60 days) is recorded as not scored. NSE/BSE plans exit by the configured square-off time (`TRADINGAGENTS_INTRADAY_SQUARE_OFF`, default 15:10 IST; set your broker's). Plans are paper only: nothing is sent to a broker.

```bash
tradingagents --horizon intraday --exchange nse --ticker TCS --analysts market,news
```

In Claude Code, opened in this folder, the `/day-trader` skill runs the same headless analysis for one NSE or BSE stock and summarises the plan from the saved report: `/day-trader RELIANCE`, or `/day-trader TCS bse`. It runs without a terminal, so first set the provider, model, language and round variables from [CLI Usage](#cli-usage) and the provider's key in `.env`; set `TRADINGAGENTS_PYTHON` to this repository's Python if an older `tradingagents` is on your PATH. See [docs/intraday.md](docs/intraday.md) for the cut-off and session rules, broker square-off times, charges and leverage, Yahoo's intraday limits, and how plans are scored.

### Web research through Agent-Reach (optional)

With [Agent-Reach](docs/agent-reach.md) installed separately and switched on, company and global news come from Exa's web search first, falling back to Yahoo when it fails or finds nothing dated in the window, and the News Analyst can also read web pages, list YouTube coverage and read a video's captions. It needs no account or key, and only its no-login channels are used; backtests never use it. Install it the isolated way the page describes, never from the PyPI package of that name, and never run its `setup` or `update`.

```bash
export TRADINGAGENTS_AGENT_REACH=1
export TRADINGAGENTS_AGENT_REACH_COMMAND=~/.tradingagents/agent-reach/env/bin/agent-reach
tradingagents --ticker RELIANCE.NS --analysts market,news
```

### Paper trading on AlphArena (optional)

[AlphArena](docs/arena.md) is a public arena where trading agents compete with simulated money on US stocks and ETFs. With `TRADINGAGENTS_ARENA=1` (or `--arena`) and your own `ALPHARENA_API_KEY`, the CLI shows a swing decision on a US stock or ETF, dated today in New York, as a paper trade after the run, and sends it only when you confirm (`--arena-yes` sends without asking, but does not turn the arena on: with the arena off, the CLI says before the analysis that it will send nothing). Before the analysis it says when the decision could not be sent, and the run goes on; when it could, a missing `ALPHARENA_API_KEY`, or one not for `TRADINGAGENTS_ARENA_MODE` (`sandbox`, the default, takes an `aa_test_` key; `live` an `aa_live_` key), stops the run with exit code 2. `--no-arena` turns the arena off for one run. A US ticker's default date is the earlier of your machine's date and New York's, so from India after midnight it is New York's; on a machine whose date is behind New York's (California after 21:00), no run can be sent until the machine's midnight. Indian listings are never sent, since SEBI has cautioned against public paper-trading platforms built on Indian listed-stock prices, and neither are intraday plans or backtests. You register with AlphArena and accept its terms yourself; start with its sandbox key.

```bash
tradingagents --ticker AAPL --analysts market,news --arena
tradingagents arena status                 # your agent's public profile and P&L (TRADINGAGENTS_ARENA_AGENT_ID)
tradingagents arena leaderboard --period 7d
tradingagents arena trades                 # your open paper trades (needs ALPHARENA_API_KEY)
tradingagents arena close AAPL             # close your open AAPL paper trades, once you confirm
```

See [docs/arena.md](docs/arena.md) for its risks, what is sent and what never is, and the ledger of every trade sent.

## TradingAgents Package

### Implementation Details

TradingAgents is built with LangGraph for flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, Azure OpenAI for enterprise and your own ChatGPT plan, and this fork adds your own Claude Pro or Max plan through Claude Code.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
state, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)

# the same report tree the CLI saves, under results_dir/reports
ta.save_reports(state, "NVDA")
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-6-sol"    # Model for complex reasoning
config["quick_think_llm"] = "gpt-6-luna"   # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
print(decision)
```

The quick model serves the analysts, researchers, debaters and trader; the deep model serves the research and portfolio managers. Each can run on its own provider, for example the managers on Claude while the rest run on OpenAI:

```python
config["deep_think_provider"] = "anthropic"
config["deep_think_llm"] = "claude-opus-5-5"
```

A tier on its own provider uses that provider's key and default endpoint; set `quick_think_backend_url` or `deep_think_backend_url` for a local or relay endpoint. The `TRADINGAGENTS_DEEP_THINK_PROVIDER` and `TRADINGAGENTS_QUICK_THINK_PROVIDER` variables set them for the CLI, together with the tier's model variable.

For an NSE or BSE stock, pass its Yahoo ticker (`RELIANCE.NS`, `RELIANCE.BO`). Setting `decision_horizon` to `"intraday"` plans one session instead of rating a position ([intraday plans](#intraday-day-trader-plans)); Yahoo serves 5-minute bars for only about the last 60 days:

```python
config = DEFAULT_CONFIG.copy()
config["decision_horizon"] = "intraday"
ta = TradingAgentsGraph(["market", "news"], config=config)   # the Day Trader is added
state, decision = ta.propagate("RELIANCE.NS", "2026-10-28")    # a past date plans the next session (29 Oct)
print(state["intraday_plan"])                                  # the plan, as data
```

See `tradingagents/default_config.py` for all configuration options.

### Fundamentals as filed

US company statements come from SEC EDGAR, which records the date every figure was filed. A run dated in the past reads the statements exactly as they stood that day: a fiscal year that has ended but has not been filed yet is not served, and a figure restated later still reads as first reported. Apple's 2008 total assets were filed as $39.6B and restated to $36.2B in 2010, so a run dated in between reads $39.6B. EDGAR needs no account or API key.

Other companies' statements come from Yahoo Finance, which dates a statement by the period it covers rather than by when it was published. A run dated today reads them; a run dated in the past is told they are withheld, since Yahoo cannot say which figures were public by then. Each is headed with its source, its unit (full units, not thousands, millions or crores) and, when Yahoo states it, the currency the company reports in, which can differ from the listing's: INFY.NS quotes in INR but reports in USD. Otherwise the header says that Yahoo does not state the currency or, when Yahoo's company profile did not load (a rate limit or an outage), that it could not be retrieved.

Insider trades are dated by when they happened, not when they were filed, so a run dated in the past is told they are withheld as well.

SEC asks callers to identify themselves and refuses requests that carry no contact address, so a default one is sent. Set your own so SEC can reach you rather than the project:

```bash
SEC_EDGAR_USER_AGENT="Your Name your@email.com"
```

It covers companies that file with the SEC, including foreign companies listed in the US. Anything else, such as Hong Kong or A-share listings, falls through to the next vendor in the chain. EDGAR's machine-readable filings begin in 2009, and a fourth quarter is reported as unavailable rather than derived, because filers publish it only inside the annual figure.

### Current holdings

By default the agents do not know what you hold, so their guidance is written for a reader who applies it to their own position. Pass a portfolio to have the trader, the risk analysts and the portfolio manager work against your actual book.

```python
from tradingagents.portfolio import PortfolioContext

portfolio = PortfolioContext.model_validate({
    "cash": 25000.0,
    "currency": "USD",
    "positions": [{"ticker": "NVDA", "quantity": 120, "average_price": 150.0}],
})
_, decision = ta.propagate("NVDA", "2026-09-01", portfolio=portfolio)
```

The CLI takes the same content as a JSON file: `tradingagents --portfolio my_book.json`.

An empty `positions` list means a flat book, which is different from passing nothing. A run without a portfolio is never treated as flat.

In this fork, a portfolio also brings the trader, the risk analysts and the Portfolio Manager risk figures computed before the run from Yahoo Finance daily prices up to the run date: each holding's value at its last close, volatility and maximum drawdown over the last year (or over the sessions Yahoo has, which the figures then state), beta and 1-year return against the run's benchmark (on an intraday run, the analyzed listing's index: NIFTY 50 or SENSEX, else its `benchmark_map` index, SPY for a US listing, whatever `--benchmark` or `TRADINGAGENTS_BENCHMARK_TICKER` says), and unrealized P/L when an average price is given; the book's concentration and sector exposure; and a sizing reference for the analyzed ticker that risks `risk_per_trade_pct` of the book (default 1%) with a stop 2 x ATR(14) below the last close, keeping the position after the trade, what is already held included, within `max_position_pct` (default 20%; `TRADINGAGENTS_RISK_PER_TRADE_PCT`, `TRADINGAGENTS_MAX_POSITION_PCT`), or n/a when that leaves no room. While an NSE/BSE session is trading, its prices are shown as latest prices, not closes: they value a holding and set the sizing reference's entry, while volatility, drawdown, beta, return and the ATR use closes through the last complete session. Set `config["portfolio_risk"] = False` to give them the holdings alone. For NSE/BSE listings the figures also count trading charges and capital-gains tax ([docs/india.md](docs/india.md)).

No exchange rate is applied. Yahoo prices an NSE/BSE listing in INR, a plain US listing in USD and a crypto pair in the currency or coin it is quoted in (BTC-INR in INR, ETH-BTC in BTC), whatever the book's label; any other holding's prices are taken to be in the book's `currency`, so set it. A book whose holdings and cash are in more than one currency, or that names no `currency` and holds a listing whose currency is not known here (7203.T, say), gets no totals, concentration or sector exposure, and its sizing reference is n/a, with the reason. The sizing reference is also n/a for an index, for a ticker whose currency is not known here, and for one that trades in another currency than the book (an INR book on AAPL, a USD book on RELIANCE.NS or on ETH-BTC). Prices per unit (an average price, a last close or latest price, and the sizing reference's entry and stop) keep six significant digits and at least two decimals, as the market tools show them: 0.0289 BTC, or XRP-BTC's 0.000016133 BTC. Totals, values and P/L, in a coin too, are shown with two decimals. Yahoo quotes most London listings in pence, so one held in a book labelled `GBP` is valued 100 times too high. A holding can be listed as several lots, which add up. A past-dated run, such as a backtest given today's book, leaves out the lots whose purchase date (`acquired`, YYYY-MM-DD) is after its date, and its risk figures say how many; a run dated today counts every lot, the book being today's, so a lot dated by a calendar ahead of the listing's (an IST date on a US run after midnight in India) is held all the same. Such a run estimates tax on a sale now, so every NSE/BSE lot's holding period is counted to today's date in India, whichever ticker is analyzed; a lot dated after that gets no holding period or tax, and a past-dated run counts to its own date.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Memory log

The memory log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`, unless the log already holds one for that ticker, run date and horizon (swing or intraday): such a second run is not logged, and it warns that its decision will not be settled or scored. A swing decision is stored with the benchmark it is measured against: the run's own (`--benchmark`, `TRADINGAGENTS_BENCHMARK_TICKER`), else its listing's regional index. While the analysts of a later run work, TradingAgents settles every logged decision whose holding period has passed: it fetches the realised return (raw, and alpha against the benchmark stored with the decision, or its listing's regional index when none is, never the settling run's) and generates a one-paragraph reflection. A decision on the benchmark itself (SPY, NIFTY 50, SENSEX) has an alpha of zero by construction, so its reflection is told to judge the raw return. The Portfolio Manager then reads the most recent decisions for the same ticker plus recent lessons from other tickers, so each analysis carries forward what worked and what didn't. A run dated in the past reads only lessons whose outcome was known by its date: an outcome from that same day counts only when its listing and the benchmark it was measured against both close together with the run's listing (NSE with BSE, US listings with each other, one exchange's listings and its index with each other), and otherwise from the next day, as does one measured against SPY on a listing whose exchange `benchmark_map` does not name, or on an NSE listing run with `--benchmark SPY`. If settling fails, the run goes on and its report says so; a decision whose stored benchmark has no close on or before its entry session (a symbol listed later) is reported that way rather than left pending. A decision logged before benchmarks were recorded (by 0.7.1, say) has the benchmark it is settled against written into its entry, so its lesson names it, and a backtest summary (`summarize`) counts one measured against itself as unscored rather than as a missed call. A swing lesson already settled without a `BENCHMARK:` line, by 0.7.1 or by this fork up to 27e6744, counts as measured against the benchmark those versions used with no `benchmark_ticker` set, the default `benchmark_map` index of its suffix, else SPY, for an index too: so an index lesson they measured against SPY (`^NSEI`, `^N225`) reaches a past-dated run on that exchange's listings only from the day after it resolved. An intraday lesson has no benchmark and goes by its listing alone. An intraday plan is scored instead on its session's 5-minute bars, once that session's close is final and while Yahoo still serves them (about 60 days) ([intraday plans](#intraday-day-trader-plans)).

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`. In the file, a line of decision or reflection text that would read as the log's own structure (`DECISION:`, `REFLECTION:` or `<!-- ENTRY_END -->` alone on its line, spaces aside) is stored behind a backslash; a decision logged by an earlier version is escaped this way when it settles.

To settle decisions without running an analysis, for a scheduled job, call `ta.settle_all_pending()`; it returns the decisions it settled and any it could not.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. The run view says whether it resumed a saved run or started fresh. Checkpoints are cleared automatically on successful completion. Checkpoints saved by v0.7.1 are not resumed by this fork: such a run starts fresh.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents --checkpoint           # enable for this run
tradingagents --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-09-01")
```

## Evaluating decisions over time

One run gives one decision, which cannot tell you whether the system decides well. `run_backtest` runs the same pipeline over a grid of tickers and dates, writes to a memory log of its own, and scores the decisions whose holding window has since traded.

```python
from tradingagents.backtest import iter_grid, run_backtest, summarize

dates = iter_grid("2026-06-01", "2026-08-01", every_n_days=7)
result = run_backtest(["NVDA", "AAPL"], dates, config, selected_analysts=["market", "news"])
print(summarize(result).render())
```

From the CLI:

```bash
tradingagents backtest NVDA,AAPL --start 2026-06-01 --end 2026-08-01 --every 7
```

The CLI sweep asks nothing; it takes its settings from the `TRADINGAGENTS_*` variables. A model tier whose provider is not `openai` (set by `TRADINGAGENTS_LLM_PROVIDER` or the tier's own provider variable) needs its `TRADINGAGENTS_QUICK_THINK_LLM` or `TRADINGAGENTS_DEEP_THINK_LLM`, or the sweep stops before its first cell; on `openai`, a tier without one keeps its built-in model.

Each cell is scored on realized alpha against the instrument's regional benchmark, or the one `benchmark_ticker` (`TRADINGAGENTS_BENCHMARK_TICKER`) sets, grouped by rating; a cell on an instrument that is its own benchmark (NIFTY 50, SENSEX, SPY) is counted as unscored, with a note, since its alpha is zero by construction. When `benchmark_ticker` is set, each cell needs a close of it in the 30 days to the cell's date (an Indian index with a daily history or a `benchmark_map` value is taken without one for a cell dated in the last 30 days): a cell dated before that benchmark's Yahoo history begins, such as a new ETF's, or NIFTY Financial Services' before 2011-09-07, is listed among the sweep's failures with the reason, and the other cells run. That check costs a CLI sweep at most two Yahoo requests: one when it starts, and one from its earliest cell's date. A decision is entered at the first close after its date, because a run reads news published after that day's close. Your own memory log is never written to, and re-running the same grid with `run_id=result.run_id` skips the cells that already ran, so an interrupted sweep continues where it stopped.

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect. The variation comes from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-6 family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date. Pin the analysis date to hold the price and indicator window fixed, but the social and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it more repeatable. The current curated models are reasoning-first and largely ignore temperature, so for tighter reproducibility name a non-reasoning model in your config, or in `TRADINGAGENTS_DEEP_THINK_LLM` and `TRADINGAGENTS_QUICK_THINK_LLM`. Any model ID your provider serves is accepted, whether or not the picker lists it.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, name a
# non-reasoning model in deep_think_llm / quick_think_llm.
```

What does not vary anymore: the analyzed company identity is resolved deterministically from the ticker before any agent runs, and the market analyst grounds exact price and indicator claims in a verified data snapshot. Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.

## Contributing

Contributions are welcome: bug fixes, documentation, and feature ideas; past contributions are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

If *TradingAgents* helps your work, please cite the paper by Xiao, Sun, Luo and Wang:

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
