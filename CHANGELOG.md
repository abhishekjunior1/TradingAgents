# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Changes that need action when upgrading are listed under "Upgrading from" in their release.

## [Unreleased]

### Added

- **Indian markets (NSE/BSE).** Indian tickers as brokers write them and index names; `--exchange`, `--benchmark` and `--vendor` options; IST sessions with the 2026 NSE/BSE holidays, special sessions and closing-auction hours; a market-context tool (benchmarks, the closest NIFTY sector index, India VIX, USD/INR, Brent) and a corporate-actions tool; India-focused news searches, Indian subreddits, StockTwits and Alpha Vantage symbol spellings, and India macro series from FRED; INR portfolios with configurable charges and capital-gains tax; the `market` command and watchlists; reports that state their market and that they are research, not advice. `--benchmark` sets the benchmark a swing run's own decision is measured against (an NSE/BSE run's agents are told it); a `--vendor` chain that leaves one of its category's tools unserved (`fundamental_data=sec_edgar` alone, or Alpha Vantage without its key) is refused before the run, and `backtest --watchlist` takes only a list's name. Corporate-action dividends are on Yahoo's present-day share basis, as its prices are, so a past-dated run's can be below the amounts declared (after a later split or bonus issue) or above them (after a later share consolidation), and its label says so in the same words whatever came after its date; a results date Yahoo still lists once it has passed is labelled the latest, not the next, and one on the run date (or a window including it) says the results may not be out yet. A charges or capital-gains setting that cannot be used (an unknown `cost_profile` or `capital_gains_rules`, an unknown field in `cost_overrides` or `capital_gains_overrides`, a `long_term_after_months` of 12.5) stops any run before a model is called, and ends a CLI run with its reason on one line, not a traceback; `TRADINGAGENTS_COST_PROFILE`, read in any case, is checked at start-up, and another name stops the run there, naming the variable. See docs/india.md.
- **Scenarios in every decision.** For any market, the Portfolio Manager's decision also gives bull, base and bear cases, key risks and a confidence level.
- **Portfolio risk figures.** A run given a portfolio, in any market, gives the trader, the risk analysts and the Portfolio Manager each holding's volatility, drawdown, beta and return against the benchmark, the book's concentration and sector exposure, and, for the listings named below, a sizing reference for the analyzed ticker (`risk_per_trade_pct`, and `max_position_pct`, which counts what is already held; `portfolio_risk` switches them off). No exchange rate is applied, and a holding off NSE/BSE, the US and crypto pairs is taken to be priced in the book's `currency`: a book in several currencies, or with no `currency` and such a holding, gets no totals, concentration or sector exposure, and the sizing reference is given only for an NSE/BSE listing, a plain US listing or a crypto pair (priced in the currency or coin it is quoted in: BTC-INR in INR, ETH-BTC in BTC) in a book of that currency, never for an index. Yahoo quotes most London shares in pence (GBp), so one held in a book labelled GBP is valued 100 times too high. A past-dated run, such as a backtest given today's book, leaves the lots `acquired` after its date out of the holdings and figures the agents see, and of the Day Trader's paper capital; a run dated today counts every lot, including one dated a day after it on a calendar ahead of the listing's (an IST date on a US run dated by New York's day).
- **Claude through a Claude plan.** The `claude_code` provider runs each model call through the person's own Claude Code sign-in.
- **Intraday (day-trader) plans.** `--horizon intraday` (or `TRADINGAGENTS_DECISION_HORIZON`) plans one trading session instead of rating a position. A new Day Trader analyst reads 5-minute bars served only up to the run's cut-off: opening range, VWAP, gap, relative volume, pivots, ATR, and paper sizes with charges and a leverage cap. The Trader and Portfolio Manager then write a paper plan with direction, entry, stop, target, exit-by time and size; NSE/BSE plans exit by the configured square-off time (`intraday_square_off`, default 15:10 IST; set your broker's). A later run scores the plan on its session's 5-minute bars once the close is final (the whole session for a run dated in the past), the stop counted first and charges included, and reflects on it. A plan made during its session is scored and reflected on from the time it was made, one made after the square-off is scored as not triggered, and a fill whose bar closes at or past the target exits at the target in that bar. The saved decision shows the plan as it is scored, with a Plan Checks line for what was moved, noted or could not be read. Its charges are configurable: `intraday_cost_profile` (`TRADINGAGENTS_INTRADAY_COST_PROFILE`, default `india_equity_intraday`, dated example rates) and per-field `intraday_cost_overrides` for your broker's rates. The date defaults to today; crypto and backtests refuse the intraday horizon, and swing runs are unchanged. The saved run settings name every run's horizon. See docs/intraday.md.
- **The `/day-trader` skill.** In Claude Code, `/day-trader TICKER [nse|bse]` runs a headless intraday analysis of one NSE or BSE stock and summarises the plan from the saved report, quoting only its numbers. It runs only when typed, and pre-approves only its own script. The script runs `TRADINGAGENTS_PYTHON`, else the `tradingagents` command, else the repo's `.venv/bin/python`, else `python3`, and stops with a message when a Python it would use is older than 3.11; the message suggests the repo's `.venv/bin/python` only when the Python it refused is not that one, under any name or path.
- **Web research through Agent-Reach (optional).** With a separately installed Agent-Reach v1.6 and `TRADINGAGENTS_AGENT_REACH=1`, news comes from Exa's web search first, falling back to Yahoo when it fails or finds nothing dated in the run's window, and the News Analyst can read web pages and YouTube coverage and captions. Only five read-only, no-login actions run, in their own HOME with no keys in their environment; results are kept only when dated within the run's window (undated ones only on runs dated today) and are labelled as untrusted. A page dated by day alone counts as that day and a placeholder date such as 0001-01-01 as undated; a time with no time zone is shown on the date the page states, as `read_web_page` shows it, and counted on the later of that date and its date in the listing's zone read as UTC, so a past run never reads it early; and `read_web_page` leaves out images and shows links as their text, so link and image markup no longer uses up its character limit before the article. Backtests never use it, a Python config that switches it off is obeyed even when the environment switched it on, and the saved run settings record whether it was on. See docs/agent-reach.md for the isolated install.
- **Paper trading on AlphArena (optional).** With `--arena` or `TRADINGAGENTS_ARENA=1` and your own `ALPHARENA_API_KEY`, the CLI offers a swing decision on a US stock or ETF, dated today in New York, to AlphArena as a paper trade with simulated money, and sends it only once you confirm, or without asking with `--arena-yes` (for runs without a terminal); the target, stop and confidence sent come from the agents' structured output when they give one, not from lines quoted in their text. Before the analysis the CLI says when the decision could not be sent, or that `--arena-yes` without the arena switched on sends nothing, and stops with exit code 2 when `ALPHARENA_API_KEY` is missing or is not a key for `TRADINGAGENTS_ARENA_MODE`. Every open and close sent goes into a local ledger, and `tradingagents arena status|leaderboard|trades|close` show and close the agent's paper trades. Indian listings, intraday plans and backtests are never sent. See docs/arena.md.

### Upgrading from 0.7.1

- Checkpoints saved by 0.7.1 are not resumed: the run starts fresh.
- A swing decision logged by 0.7.1 and still pending is settled against its listing's benchmark (SPY for a US ticker), not `benchmark_ticker`: 0.7.1 recorded no benchmark with a decision, and a run's own no longer applies to other runs' decisions. When it settles, that benchmark is written into its entry (a `BENCHMARK:` line), so its lesson names it and the backtest summary counts a decision on its own benchmark as unscored.
- `benchmark_ticker` (`TRADINGAGENTS_BENCHMARK_TICKER`) is checked before a swing run; an intraday run, whose plan has no benchmark, neither checks nor uses it. India VIX, VIX at every term and the other volatility indices listed in `tradingagents/memory/settlement.py` (other Cboe indices, MOVE, and the Hang Seng, Nikkei 225, ASX 200 and VSTOXX ones) are refused, an Indian index must be one Yahoo keeps a daily history for (not NIFTY Midcap 100, say), and a symbol that is neither that nor a `benchmark_map` value needs a Yahoo close in the last 30 days, so Yahoo must be reachable, and in the 30 days to the run's date. Such an index or `benchmark_map` value is taken without a request on a recent date, but a run or backtest cell dated more than 30 days back needs a Yahoo close of it in the 30 days to that date, so a cell before the index's Yahoo history (NIFTY Financial Services' starts on 2011-09-07) is refused. A refused value makes the CLI exit with the reason before it asks anything, naming `--benchmark` or `TRADINGAGENTS_BENCHMARK_TICKER`, whichever set the value: it checks the value at `--date`'s day, with at most one Yahoo request, whose prices the graph reuses, and refuses a date typed at the prompt in one line when the run starts. Building the graph raises too, so a backtest stops before its first cell. A past-dated run whose benchmark has no close in the 30 days to its date (an ETF listed since) raises before any model is called, and a backtest lists such a cell among its failures, with the reason.
- On a provider other than `openai`, a run without a terminal needs both `TRADINGAGENTS_QUICK_THINK_LLM` and `TRADINGAGENTS_DEEP_THINK_LLM`, and `tradingagents backtest` the variable of each tier not on `openai`: 0.7.1 took one and gave the other tier OpenAI's built-in model.

### Fixed

- A run dated New York's day on a US ticker is no longer treated as dated in the past when the machine's date is ahead of New York's (in India from midnight until 09:30 IST, 10:30 IST in US winter): it was withheld the live company profile (sector and industry included), insider trades, statements, prediction-market odds and upcoming events, read web news by a past run's rules, was refused live web pages and had its lessons filtered. "Today" for these rules is the listing's date when it is earlier than the machine's (New York's for plain US tickers, India's for NSE/BSE), never later, and the CLI's default analysis date follows it.
- A news window ending on the machine's date is no longer reported as extending past today while the machine's date is ahead of UTC's (in India, midnight to 05:30 IST).
- Yahoo and Agent-Reach news, StockTwits and Reddit windows count the listing's days: India's for NSE/BSE, New York's for a US listing, UTC's otherwise. A past NSE/BSE run read up to 5.5 hours of the next Indian day's stories (the overnight US close, GIFT Nifty, results write-ups), and a US run lost its window's last evening after 20:00 New York time (19:00 in winter); Reddit post dates are shown in the listing's days too.
- The Sentiment Analyst's news heading names the configured news vendor (Yahoo Finance or Alpha Vantage) instead of always Yahoo Finance, and heads Agent-Reach web results as unverified third-party pages, not institutional headlines.
- Polymarket gives up on a connection after 10 s instead of 30 s, and after a failure to connect at all (a connect timeout, a refusal, an unknown host, an unreachable proxy) or a timeout before it answers (a TLS handshake or a proxy's CONNECT that stalls included, which urllib3 reports as a read timeout) it is not asked again for 5 minutes: out of reach, it cost a run 30 s for each topic asked about. A server that answers, even with an error, drops the connection or rejects the TLS handshake is asked again at the next request.
- A data vendor that is not configured is logged with the reason, such as a missing API key or Agent-Reach not installed, before the next vendor is tried.
- The CLI no longer prints the user name, password or query of `TRADINGAGENTS_LLM_BACKEND_URL`, which can carry credentials: a headless run's output is kept in logs, and the `/day-trader` skill shows Claude the end of a failed run's output. They read `***`; the run still uses the URL as given.
- A US listing's daily bar cached while its session was still trading is no longer served later as that day's close when the machine's date runs ahead of New York's (in India after midnight, for example): the price cache judges "today" by the listing's own exchange date, India's for NSE/BSE and New York's for plain US tickers.
- Indicators, the verified snapshot and the market context (`tradingagents market --date` too) for a date more than about four years back now get its prices and over a year of history before it, downloaded from the start of the year 400 days before it into a `<symbol>-YFin-data-from-<year>.csv` cache file beside the usual one. They had only the last five years of prices, so an older date read as no data, a holiday or a possibly delisted symbol, and one in the first of those years had too little history before it.
- Daily prices and the verified snapshot say they are Yahoo's split- and dividend-adjusted series: rows before an ex-dividend date, even one after the dates asked for, are scaled down by that dividend, so they can sit below the exchange's printed prices and unadjusted figures such as intraday bars.
- VWMA and MFI read N/A where their 14-session window has no reported volume (Yahoo gives India VIX none) instead of 0.0 and 0.5, and MFI is on its usual 0-100 scale, the one its 80/20 guidance is written for: it was 0-1.
- NSE listings with `&` (`M&M.NS`) are valid tickers.
- Global news for an NSE/BSE listing no longer arrives as US stories under India queries: Yahoo's fuzzy search answered them with unrelated stories. It now comes in an India section (exact searches for `rupee`, `RBI` and `India`, kept only when India-related; a word that also names something outside India, such as "rupee" or "RBI", needs a second India signal) and a Global section, sharing the article limit; an empty India section says which India drivers to report as unavailable, and a section the limit or an earlier section's results leave empty says so rather than that Yahoo found nothing. A Global section over a window Yahoo's search cannot reach says the news is unavailable, and with `global_news_queries_india` emptied the India section stays: Yahoo's says no India searches are configured, and Agent-Reach's still searches `agent_reach_india_queries`.
- Insider transactions for an NSE/BSE listing carry a note when Yahoo's per-share prices do not fit the stock's INR closes: Yahoo states no currency, and they are not rupee amounts. A BSE listing Yahoo has no daily history for (RELIANCE.BO) is checked against the NSE listing under the same symbol, and rows that cannot be checked still carry the caution, saying whether Yahoo has no closes for the period or they could not be retrieved. The closes over the trades' dates are used with no staleness test and never read past the run date, so a listing whose Yahoo history stops or pauses during or after the trades (suspended, or back from a suspension) is checked against the closes it has then rather than said to have none, and a BSE listing is held to its NSE listing only when Yahoo has none for it then (or they could not be retrieved). Checking rows that the price tools' 5-year download holds costs no download of its own.
- Yahoo statements say what their numbers are: each names Yahoo Finance, the currency the company reports in (Yahoo's `financialCurrency`, read once a day per listing; when the company profile does not load, it says the currency could not be retrieved, not that Yahoo does not state it) and that amounts are full units, and warns when that currency is not the listing's (INFY.NS quotes in INR and reports in USD, so its Diluted EPS of 0.80 is dollars). Quarterly columns that are not consecutive quarters, such as RELIANCE.NS's half-yearly balance sheet or a skipped quarter, are flagged.
- `get_cashflow(freq="quarterly")` for an NSE/BSE company listing (not an index) with no data explains that Indian companies publish cash-flow statements half-yearly (SEBI LODR) and suggests `freq="annual"`, instead of suggesting the symbol may be invalid.
- Daily prices and indicators for NSE/BSE listings say what they are. A row of a session still trading is marked as such (its Close is the latest price, not a close), and an indicator read from it as provisional. An indicator computed from fewer sessions than it needs reads "not enough price history" instead of a number: on 30 Oct 2026 Yahoo served RELIANCE.BO one daily bar, which read as an RSI of 50.0. A date with no row is a named holiday, a weekend, or a gap in Yahoo's data, and the price table counts the trading days Yahoo left out; for other listings a missing weekday is no longer called a holiday. The verified market snapshot does the same (it gave RELIANCE.BO an ATR of 19.90 from one bar) and says how many daily rows back its indicators.
- Yahoo's filler row for a day NSE/BSE was closed (one price as open, high, low and close, and no volume; RELIANCE.NS and M&M.NS on the 15 Jan 2026 holiday) is no longer read as a session by daily prices, indicators, the verified snapshot, market context or settlement: the price table notes it, an indicator for that date names the holiday, and the snapshot says why the date has no row. Holidays are recognised in the calendar's years (2026), weekends in any.
- Relative performance (the market context's 1M, 3M and 1Y against its benchmark index and the sector index, the watchlist's 1M and 3M columns, and the portfolio risk figures' 1Y return against the benchmark) is measured over the last 21, 63 or 250 sessions the stock and the index both have. Each series was cut from its own rows, so a row only one of them had (Yahoo's 15 Jan 2026 filler on stocks, a day an index lacks, an index close published late) started one window on another day, moving a figure by up to several points and sometimes flipping its sign. The market context and the watchlist date them by the last close both have when the two end on different days ("to 27 Oct, the last close both have"), and a watchlist entry that is not a valid symbol (`USD/INR`, `TATA MOTORS`) gets a row of its own saying why, with "did you mean USDINR=X?" for the pair, instead of replacing the whole table with a DATA_UNAVAILABLE notice. A BSE listing's market context measures it against SENSEX, its exchange's index, as its watchlist columns do; it was measured against NIFTY 50.
- The intraday snapshot also gives the gap at the exchange's opening price (the daily bar's open, set in NSE/BSE's pre-open auction) beside the gap at the first 5-minute bar's open; they can differ by half a percent (TCS on 30 Oct 2026: +0.69% against +1.18%). It is measured against Yahoo's previous daily close, which is dividend-adjusted, and labelled so, with a note: on an ex-dividend day it reads higher than the gap as traded, by about the dividend yield. When the bars hold no session before the plan's, the last session and its pivots come from the daily bar too, the gap at the first bar's open is measured against that bar's close, and each is labelled dividend-adjusted. An index (a caret symbol, or an Indian or default `benchmark_map` index Yahoo lists without one, such as NIFTY_FIN_SERVICE.NS, 000001.SS, 399001.SZ or FTSEMIB.MI), a currency pair, a future or a crypto pair pays no dividend, so none of its figures is labelled dividend-adjusted.
- A swing rating and an intraday plan for the same ticker and date are both kept in the memory log and each settled its own way: the second was dropped without a word, so an intraday plan made after a swing run on the same stock that day was never scored. A run whose decision is not added (the same ticker, date and horizon already logged) now says so.
- Each swing decision is measured against the benchmark of the run that made it, which the memory log keeps with it (a `BENCHMARK:` line): a run's `benchmark_ticker` (`--benchmark`, `TRADINGAGENTS_BENCHMARK_TICKER`) was applied to every due decision it settled, whichever run made it, and not kept with its own. Lessons name the benchmark ("+1.0% vs ^CNXIT"), and the saved run settings and the report header, in complete_report.md and complete_report.html alike, name one the run set ("- Benchmark: QQQ, set for this run", or on an NSE/BSE listing's Market line).
- An NSE/BSE index analysed as the ticker (`^NSEBANK`, `^CNXIT`) is measured against its exchange's headline index, NIFTY 50 or SENSEX, instead of SPY. A decision on an instrument that is its own benchmark (NIFTY 50, SENSEX, SPY) has an alpha of zero by construction: its reflection is told to judge the raw return, and a backtest counts its cells as unscored, with a note, instead of reporting "called the direction 0%". A pending decision logged before benchmarks were recorded has its benchmark written into its entry when it settles, so one on NIFTY 50, SENSEX or SPY is counted the same way.
- An intraday run's portfolio risk figures (beta and 1-year return), and for a listing outside NSE/BSE its market context's Benchmark row, use the listing's index (NIFTY 50 or SENSEX, else the `benchmark_map` index, SPY for a US listing), as do `tradingagents market` watchlist rows off NSE/BSE while `TRADINGAGENTS_DECISION_HORIZON` is `intraday`: they used `--benchmark` or `TRADINGAGENTS_BENCHMARK_TICKER`, which an intraday run does not check and its plan does not have. Given `--benchmark`, the CLI now says it does not apply to an intraday run.
- Model text can no longer split or forge a memory-log entry: a line of a decision or reflection that reads `DECISION:`, `REFLECTION:` or `<!-- ENTRY_END -->`, whatever spaces surround it, is stored behind a backslash. Such a line could plant a fake settled lesson that a backtest's date filter let through, cut short the decision a reflection was written on, or, at the end of a decision, lose its reflection when it settled. A decision logged before this is escaped when it settles, so one that ends in such a line with a space or tab after it keeps its reflection; settled entries are not rewritten.
- A run dated in the past, such as a backtest, reads a lesson resolved on its own date only when the lesson's listing and the benchmark it was measured against both close with the run's listing: NSE with BSE, US listings together, one exchange's listings and its index together (one Yahoo suffix, such as `.T`, with `^N225`); any other counts from the next day, such as one measured against SPY, the default for an index outside India and for a listing whose exchange `benchmark_map` does not name, or one on an NSE listing run with `--benchmark SPY`. A swing lesson that 0.7.1, or this fork up to 27e6744, settled without a `BENCHMARK:` line counts as measured against what those versions used with no `benchmark_ticker` set, the default `benchmark_map` index of its suffix, else SPY, for an index too, so an index lesson they measured against SPY (`^NSEI`, `^NSEBANK`, `^N225`, `^FTSE`) reaches a past-dated run on that exchange's listings only from the day after it resolved; an intraday lesson goes by its listing alone. An NSE run read a US outcome known only after 01:30 IST the next morning, a Tokyo run same-day US, London and coin outcomes known up to 17.5 hours after its close, and a NIKKEI 225 run an alpha against SPY's close of that day, final 13.5 hours after Tokyo's.
- A run without a terminal (a scheduled job, the `/day-trader` skill) no longer replaces the answers the interactive CLI offers back next time.
- On a provider other than `openai`, a run needs both `TRADINGAGENTS_QUICK_THINK_LLM` and `TRADINGAGENTS_DEEP_THINK_LLM`: with one set, the other tier silently got OpenAI's built-in model (`gpt-6-luna` or `gpt-6-sol`). Without a terminal the run now stops before it starts and names the missing one, at a terminal only that tier is asked for, and `tradingagents backtest`, before its sweep, refuses a tier off `openai` without its variable; runs on `openai` are unchanged.
- The CLI shows error text as written: bracketed details, such as pydantic's `[type=missing, ...]`, were dropped from setup, portfolio, report-saving and backtest messages, and a value such as `--ticker '[/x]'` crashed the CLI. Given to `tradingagents backtest`, such a ticker ended the sweep at its progress line, before the settlement pass and the summary; it now fails as its own cell.
- The `tradingagents market` and `tradingagents arena` tables no longer cut dates, prices and percentages short ("2026-1…", "1,988…"), at a narrow terminal or without one (a pipe, a log, Claude Code's Bash tool): a table that cannot fit even with its long names wrapped, such as the watchlist at 80 columns, is shown row by row, and without a terminal tables print at full width.
- A portfolio's holdings are shown in full: 12,345 shares read "12,345 units", not "1.234e+04 units", and a fractional amount keeps up to 8 decimals (1.23456789, not 1.235).
- Prices per unit in a portfolio (a holding's average price, its last close or latest price, and the sizing reference's entry and stop) keep six significant digits and at least two decimals, as the market tools show them: printed with two decimals, XRP-BTC's 0.000016133 BTC read 0.00 BTC and ETH-BTC's 0.0289 read 0.03. Totals, values and P/L keep two decimals.
- On a run dated today, an NSE/BSE lot's holding period, and with it the term and estimated tax on a sale now, is counted to today's date in India, whichever ticker is analysed. A run dated by a day behind India's (a US run dated by New York's day after midnight IST, or a run on a machine whose date is behind India's) counted it to that day, so the term and tax of the same book at the same moment depended on the ticker analysed and the machine's date, and a lot could reach long-term a day late. A lot dated after today in India gets no holding period or tax, and its line says so ("purchase date ... is after ... (today in IST)"); a past-dated run still counts to its own date.
- A crypto pair quoted in a currency other than the US dollar, or in a coin (BTC-INR, BTC-EUR, ETH-BTC, SOL-ETH, PEPE-USDT), is no longer read as a US listing priced in USD. Its prices are in the currency or coin it is quoted in; like other crypto, it is dated by the machine's date, its news windows counting UTC's days, not New York's; and `--exchange` leaves it as written, for the ticker and in a portfolio file, instead of reading it as an NSE or BSE listing (`ETH-BTC.NS`). A USD book analysing ETH-BTC was given a sizing reference for its BTC price read as dollars, and a holding of it was added into the book's USD total.
- yfinance 1.7 no longer fills a run's output with pandas 3.1's deprecation notices ("Pandas4Warning: The inplace keyword ... is deprecated"); `PYTHONWARNINGS` could not hide them, because yfinance shows deprecation warnings from its own modules.

## [0.7.1] — 2026-10-27

Decisions are scored only on final closes and settle across long market holidays, a model's unpadded date no longer reads filings made after the run date, and fixes to portfolios, prices, SEC statements, reports, Azure and Claude settings.

### Upgrading from 0.7.0

- Docker images no longer include `.env.enterprise`. For Azure OpenAI in Docker, put its keys in `.env`.

### Fixed

- A date a model passes without zero padding, such as `2024-7-5`, is read as that date. It could compare as later than a filing made after the run date, and SEC EDGAR then served that filing. (#1488)
- A decision is no longer scored on the bar of a session still trading, and one made before a market closure longer than a week, such as a national holiday, settles once trading resumes instead of staying pending. (#1548, #1539)
- A backtest reports the failures of its last settlement pass, and two started in the same second no longer share one run. (#1557, #1549)
- A holding listed under a broker's spelling (`BTCUSDT`, `700.HK`) is matched to the instrument analyzed, and a portfolio number that is not finite is refused when the file loads. (#1556, #1509)
- Prices keep their significant digits: a sub-cent close no longer prints as 0.00, and a currency pair keeps its pips. (#1567)
- SEC EDGAR statements list one row per period, so a model no longer reads a figure against the wrong quarter across a long history, and a figure under one million no longer prints as 0. (#1517, #1589)
- Saving a report to a folder used before removes the earlier run's sections. (#1566)
- A date window that ends before it starts is sent back to the model instead of returning no data. (#1595)
- A statement frequency other than annual or quarterly is sent back to the model instead of being served annual figures under that word. (#1593)
- The memory's most recent lessons, and the entries rotation keeps, follow decision dates, so a backfill of older dates no longer displaces them. (#1603)
- Prediction-market odds near 0% or 100% show their distance from it to two significant digits. (#1598)
- Azure OpenAI receives `max_tokens` and a configured endpoint. (#1584, #1600)
- Claude's effort setting is decided correctly for dated model IDs such as `claude-sonnet-4-20250514`. (#1585)
- The CLI checks a ticker with the same rule as the run, so `XAUUSD+` is accepted and `..` is refused at the prompt. (#1604)
- Docker images no longer include `.env` files from any folder.
- Tests that failed on Windows and macOS for platform reasons are fixed. (#1506, #1485, #1541)

### Contributors

[@alanfate3](https://github.com/alanfate3), [@aranlucas](https://github.com/aranlucas), [@BichengWang](https://github.com/BichengWang), [@Bortlesboat](https://github.com/Bortlesboat), [@hoffman988](https://github.com/hoffman988), [@jhill-cmd](https://github.com/jhill-cmd), [@muhib-karim](https://github.com/muhib-karim), [@pei711](https://github.com/pei711), [@peiji1981](https://github.com/peiji1981), [@rudycelekli](https://github.com/rudycelekli), [@Shubham-Padkonde](https://github.com/Shubham-Padkonde), [@stevexyz2003](https://github.com/stevexyz2003), [@SuperMarioYL](https://github.com/SuperMarioYL), [@sytfly](https://github.com/sytfly), [@yuv009](https://github.com/yuv009).

## [0.7.0] — 2026-10-16

Runs on your ChatGPT plan, backtests scored from the first close after each decision, a note at start-up when a newer release is out, and a wheel attached to each GitHub release.

### Added

- **Your ChatGPT plan.** The `chatgpt` provider runs TradingAgents on a ChatGPT Plus or Pro plan instead of an API key, through OpenAI's sign-in for open-source apps. Choose "Subscription" in the CLI's provider menu; for unattended runs, Docker and Python (`llm_provider="chatgpt"`), sign in once with `tradingagents login chatgpt`. `tradingagents logout chatgpt` revokes the sign-in. The CLI lists the plan's models, and reasoning effort applies as it does for OpenAI. (#1194, #1475)
- **What's new at start-up.** The interactive CLI mentions a newer release, with the command that upgrades this install when it can tell how TradingAgents was installed, and notices from Tauric Research for the running version. It checks at most once a day in the background, so a run never waits on it; runs without a terminal never check, and `TRADINGAGENTS_NO_UPDATE_CHECK=1` turns it off.
- **Release downloads.** Each GitHub release carries the source archive and a wheel, so a version installs with `pip install` from its release page without cloning.

### Upgrading from 0.6.0

- `cryptography` 42 and `packaging` 24 or later are installed.
- Decisions are now scored from the first close after their date. Outcomes already settled in a memory log keep the earlier entry, the close of the decision date, so a backtest resumed with `run_id` mixes the two; start a new run to compare like with like.

### Fixed

- A decision is no longer scored against news it could not have traded on: a run dated *d* reads news published after the close on *d*, so the memory log and backtests enter it at the first close after *d*, not the close of *d*. (#1518)
- A run without a terminal, such as a scheduled job or CI, no longer contacts the announcements server before it starts.
- A model tier on another provider keeps its configured reasoning effort, thinking level or Claude effort when the main provider has none. (#1440)

### Contributors

[@LeonWTW](https://github.com/LeonWTW), [@OleksanderSS](https://github.com/OleksanderSS), [@sovorn-c](https://github.com/sovorn-c).

## [0.6.0] — 2026-10-03

HTML reports, a provider per model tier, past decisions settled for every ticker while the analysts work, and company news read while Yahoo's news feed is down.

### Added

- **HTML report.** A saved report also includes `complete_report.html`, the report as one page with its sections listed beside the text, that reads well in a browser, on a phone and in print. Answering the save question at the prompt also asks about the page and can open it in your browser; `--no-html` or `save_reports(..., html=False)` skips it. The page is self-contained: it loads nothing and runs nothing. (#1419)
- **A provider per model tier.** The quick and deep models can run on different providers, for example the managers on Claude while the analysts run on OpenAI: set `deep_think_provider` or `quick_think_provider` (`TRADINGAGENTS_DEEP_THINK_PROVIDER`, `TRADINGAGENTS_QUICK_THINK_PROVIDER`) with the tier's model. The report header names each tier's provider. (#1440)
- **Past decisions settle alongside the analysts.** While a run's analysts work, every logged decision whose holding period has passed is settled, for any ticker, not only the one being analyzed. If settling fails, the run goes on and the report header says so. (#1428, #1445)
- **Crypto on Reddit.** For a crypto ticker the Sentiment Analyst reads r/CryptoCurrency and r/CryptoMarkets, plus r/Bitcoin, r/ethereum or r/solana for those coins, instead of the stock subreddits. (#1461)
- `settle_all_pending()` settles every due decision without running an analysis, for a scheduled job; it and `settle_pending()` return the decisions settled and any that could not be. (#1445)
- The report header names the run's rating.
- Euro-area macro series by name: `ecb_deposit_rate`, `ecb_main_refi_rate`, `euro_hicp`, `euro_core_hicp`, `euro_real_gdp`, `germany_10y`, `france_10y` and `eur_usd`. (#1465)

### Models

- A Gemini call times out after 600 seconds, as OpenAI and Anthropic calls do, so a stalled call no longer hangs the run. (#1417)

### Upgrading from 0.5.2

- markdown-it-py 4.0 or later is installed.
- A saved report also writes `complete_report.html`; pass `--no-html`, or `html=False` to `save_reports()`, to keep only the Markdown.
- A run no longer settles its ticker's decisions before it starts; the Memory Log step settles every ticker's during the run.
- `settle_pending()` returns a `Settlement` with `settled` and `failed` (was `None`).
- Checkpoints from 0.5.2 are not resumed.

### Fixed

- Company news is read from Yahoo search while Yahoo's news feed returns nothing. When Yahoo has no article about the symbol, its news is reported unavailable, not absent, and a configured next news vendor is tried. (#1467, #1469)
- A rating quoted from someone else, such as "Consensus rating: Buy", no longer replaces the Portfolio Manager's own. (#1466)
- A Yahoo news article with a null field no longer loses the ticker's news. (#1458)
- A start date a model writes with stray characters is sent back to it to correct, instead of failing the news or price request. (#1476)
- A request Alpha Vantage rejects is reported as unavailable, not served as data; the API key is kept out of every Alpha Vantage error. (#1442)
- Yahoo fundamentals print each money figure in its own currency: London prices in GBp, market cap and per-share figures in GBP, and an ADR's income figures in its reporting currency, such as TWD for TSMC. (#1456)
- Jev screening uses the endpoint `TYPESAFE_BASE_URL` names, so it can run through OpenRouter. (#1416)
- Runs sharing a memory log settle each decision once, and an interrupted settlement keeps the outcomes it finished.
- The report header names the sentiment analyst `sentiment`, as the CLI does.
- `requirements.txt` says the dependencies are declared in `pyproject.toml`.

### Contributors

[@amitbhansali](https://github.com/amitbhansali), [@AyushJain-601](https://github.com/AyushJain-601), [@BichengWang](https://github.com/BichengWang), [@byte7m](https://github.com/byte7m), [@Dangerboi](https://github.com/Dangerboi), [@davidalmeida90](https://github.com/davidalmeida90), [@Jackzigen](https://github.com/Jackzigen), [@jhill-cmd](https://github.com/jhill-cmd), [@Recnelis0](https://github.com/Recnelis0), [@seaaxe](https://github.com/seaaxe), [@vaegev](https://github.com/vaegev).

## [0.5.2] — 2026-09-29

Parallel analysts, unattended CLI runs, reports that record what produced them,
and past runs served only what was known on their date.

### Added

- **Parallel analysts.** The selected analysts run at the same time, so the analyst phase takes about as long as the slowest one; the CLI shows each analyst's progress and time as it finishes, and the debate starts when all reports are in. (#1255, #433)
- **Unattended CLI runs.** Flags answer the per-run questions, and `TRADINGAGENTS_*` variables the rest, so a scheduled job or script asks nothing: `tradingagents --ticker NVDA --date 2026-09-26 --analysts market,news --save --no-show`. Without a terminal, a missing answer stops the run before it starts and names what to set. (#1127, #1133)
- **Reports record what produced them.** Each report opens with the analysis date, version, provider, models, analysts, rounds and data vendors; the state log keeps them as `run_settings`. (#752, #1197)
- **Docker data folder.** `TRADINGAGENTS_DATA_DIR` keeps results, reports and the memory log in a host folder. (#865)
- **A cap on an analyst's tool calls.** After `max_tool_rounds` rounds (default 20, `TRADINGAGENTS_MAX_TOOL_ROUNDS`) the analyst writes its report from what it has. (#1420)
- `tradingagents backtest` shows each cell as it starts and the `--run-id` to continue; `--analysts` accepts `sentiment`.
- Benchmarks for Taiwan, Korea, Singapore and the main European exchanges. (#1392)
- Python 3.14 support; the Docker image runs Python 3.13.

### Models

- `deepseek-flash` is treated as a thinking model, so structured output works. (#1388)

### Upgrading from 0.5.1

- Python 3.11 or later is required; pandas 3 is installed.
- Minimum versions: yfinance 1.7.0, langchain-core 1.6.5, langchain-openai 1.6.6, langchain-anthropic 1.7.4, langchain-google-genai 4.4.0, langgraph 1.2.12, langgraph-checkpoint-sqlite 3.1.1, langchain-aws 1.7.9 (`bedrock` extra).
- Statements default to SEC EDGAR, then Yahoo (`fundamental_data = "sec_edgar,yfinance"`).
- A past-dated run gets no Yahoo or Alpha Vantage statements or insider trades, which carry no filing date, and only the company's current name, marked as such.
- The memory log is the `tradingagents.memory` package (was `tradingagents.decision_log`); settlement and reflection moved there from `tradingagents.graph`, and `Reflector` is no longer exported from `tradingagents.graph`.
- `process_signal` is removed; the rating is `propagate()`'s second value or `final_state["final_rating"]`.
- A rating is read only from its `Rating:` label; a free-text decision without one is `REVIEW`.
- Data-layer functions take `as_of_date=` (was `curr_date=`); `build_instrument_context` and `resolve_instrument_context` take `trade_date=`.
- `VendorRateLimitError` is `VendorUnavailableError`.
- Data tools take the instrument from the run's state; tool calls no longer carry `symbol` or `ticker`.
- State log: `trader_investment_plan` (was `trader_investment_decision`), `judge_decision` removed, `final_rating` and `run_settings` added.
- Checkpoints from 0.5.1, or saved under different settings, are not resumed.

### Fixed

- Data tools serve the run's instrument, even when the model passes another symbol.
- A failed vendor request is reported as unavailable, not as a missing or delisted symbol; one vendor's "no data" no longer speaks for a chain in which another failed. (#1386)
- The rating is the Portfolio Manager's own, not one its thesis quotes (#1383); its line keeps an English label in other output languages (#1435).
- Backtest cells given twice run once (#1415), and settlement compares a decision with its benchmark over the same dates.
- A snapshot for a symbol with no prices no longer ends the run.
- Concurrent runs no longer drop memory log entries.
- SEC EDGAR quarterly figures filed only year to date are served, labelled with their span.
- Yahoo dividend yield and debt-to-equity print as percentages. (#1414)
- The FRED change line names the dates it spans. (#1397)
- `stream_run` serves the graph's own config to its tools.
- The test suite stays off the network and out of the user's files, and passes in any order; integration tests run with `-m integration`. (#1395)

### Contributors

[@1Wizzy](https://github.com/1Wizzy), [@BichengWang](https://github.com/BichengWang), [@Chaoqi31](https://github.com/Chaoqi31), [@davidalmeida90](https://github.com/davidalmeida90), [@djconnexion77](https://github.com/djconnexion77), [@fuzing](https://github.com/fuzing), [@hailampy123](https://github.com/hailampy123), [@hesam-shams](https://github.com/hesam-shams), [@HEYALT](https://github.com/HEYALT), [@Jackzigen](https://github.com/Jackzigen), [@kagura-agent](https://github.com/kagura-agent), [@loulanyue](https://github.com/loulanyue), [@mannubaveja007](https://github.com/mannubaveja007), [@olivergpt](https://github.com/olivergpt), [@rita112025-cpu](https://github.com/rita112025-cpu), [@shuyan-code](https://github.com/shuyan-code), [@SingTheCode](https://github.com/SingTheCode), [@sjq597](https://github.com/sjq597), [@xiaodu55](https://github.com/xiaodu55), [@Youholdme](https://github.com/Youholdme).

## [0.5.1] — 2026-09-24

A package layout organised by what each module holds, social posts screened by
TypeSafe's Jev when a key is set, GPT-6 Sol and Luna as the default models, and
fixes to run isolation, SEC EDGAR statements and historical runs.

### Upgrading from 0.5.0

Some modules moved, and the old import paths are gone. Update imports as follows:

- `tradingagents.dataflows.interface` is `tradingagents.dataflows.router`, and `dataflows.symbol_utils` is `dataflows.symbols`. `dataflows.utils` is gone: `get_current_date` is in `dataflows.date_window`, `safe_ticker_component` in `dataflows.symbols`, and `get_scrubbed` and `vendor_reachable` in `dataflows.net`.
- Vendor modules live under `tradingagents.dataflows.vendors`: `yahoo` (`ohlcv`, `market`, `fundamentals`, `news`, `snapshot`, from the former `stockstats_utils`, `y_finance`, `yfinance_news` and `market_data_validator`), `alpha_vantage` (a package, from the `alpha_vantage_*` modules), and `sec_edgar`, `fred`, `polymarket`, `reddit`, `stocktwits`.
- `tradingagents.agents.utils` is gone: the agent tools are in `agents.tools`, and `agent_utils`, `agent_states`, `rating` and `structured` are `agents.context`, `agents.state`, `agents.rating` and `agents.structured`.
- The decision log is `tradingagents.decision_log` (was `agents.utils.memory`), and `cli.utils` is `cli.prompts`.
- `backtest.summarize` takes a `run_backtest` result or the path of a decision log, in place of a `TradingMemoryLog`.
- Removed: `SignalProcessor` (the rating is parsed by `process_signal`), the `create_social_media_analyst` alias (use `create_sentiment_analyst`), `symbol_utils.is_yahoo_safe`, the unused `project_dir` config key, and the graph attributes `curr_state`, `ticker` and `log_states_dict`, which held the previous run's state.

### Added

- **Jev post screening.** With `TYPESAFE_API_KEY` set, TypeSafe's Jev reads each StockTwits and Reddit post the Sentiment Analyst fetches: posts that are not about the company are dropped, and each source opens with a count of the rest by stance. Without the key nothing changes. (#1376)
- B3 tickers (`.SA`) are benchmarked against the Ibovespa. (#1366)

### Models

- GPT-6 Sol and GPT-6 Luna are the default deep and quick models, and Claude Opus 5.5 replaces Opus 5 in the picker. Opus 5 and GPT-5.4 Mini remain valid model IDs.

### Fixed

- Several graphs in one process each read their own data vendors. (#1369)
- SEC EDGAR cash flow statements find capital expenditure for filers that moved it to purchases of productive assets (NVIDIA since fiscal 2022, Amazon since 2016), whose recent periods read as empty. (#1370)
- SEC EDGAR annual statements list fiscal years only.
- A historical run is no longer told today's date, or a date after the run, in coverage notices and the instrument context.
- The Fundamentals Analyst can call the insider transactions tool.
- An unreachable Yahoo on insider transactions is reported as unavailable, not as a symbol without data.
- A graph reused across runs, as in a backtest, no longer keeps every run's full state.
- A checkpointed CLI run says whether it resumed or started fresh.
- A blank path variable (`TRADINGAGENTS_RESULTS_DIR` and the like) keeps the default path.
- StockTwits messages reach the prompt as plain text rather than HTML-escaped.
- The test suite is independent of the developer's `.env`, time zone and network. (#1368, #1372)

### Contributors

Thanks to everyone who reported these or sent a fix:

[@codify88](https://github.com/codify88), [@davidalmeida90](https://github.com/davidalmeida90), [@duongylinh](https://github.com/duongylinh), [@jccl2](https://github.com/jccl2), [@yuina368](https://github.com/yuina368).

## [0.5.0] — 2026-09-18

Point-in-time integrity across every dated path, decisions that are recorded as
they were made, backtesting over a grid of tickers and dates, the caller's
portfolio as run input, and SEC EDGAR fundamentals served as filed.

### Highlights

- **Fundamentals as filed.** SEC EDGAR serves US company statements as they stood on the run's date: a period that has ended but has not been filed is not served, and a figure restated later still reads as first reported. Keyless, opt-in via the vendor chain.
- **Backtesting.** `run_backtest` runs the pipeline over a ticker and date grid into its own decision log, and `summarize` scores the settled cells; `tradingagents backtest` does the same from the CLI.
- **Portfolio context.** `propagate(..., portfolio=...)` and `--portfolio` let the trader, risk and portfolio agents size against real holdings. A run without one is never treated as a flat book.
- **Decisions are recorded as made.** An unreadable decision is flagged for review everywhere instead of becoming a tradeable Hold, and a rating argued against is no longer read as the call.

### Point-in-time and honest attribution

- Dated tools take the run's date from graph state, so an omitted or later date cannot reach a vendor. (#1331, #1319, #1118)
- Insider filings and prediction-market odds are bounded by the run date; insider rows state that a trade becomes public when its Form 4 is filed.
- A feed that never observed a window reports it as unavailable rather than as an absence, across news, Reddit and StockTwits.
- The resolved company identity says when it describes today rather than the run date.
- The verification snapshot quotes the prices the vendor reported, never a gap-filled value.
- A vendor failure is a vendor failure: yfinance raises instead of returning its errors as text, an outage is not reported as a company with no data, and a chain where every vendor is unavailable says so instead of ending the run.
- The macro vintage pin is clamped to the vendor's own clock, so a run dated today cannot ask for a vintage it does not have.
- A historical run is not served a present-day company profile by either fundamentals vendor. (#1300)

### Decisions and evaluation

- The labelled rating decides, whatever separates it, and prose naming several ratings is reviewed rather than guessed.
- Decision prompts state the shape of their answer, so a provider without structured output still returns a readable decision.
- A report that was not produced says so, instead of appearing as an empty section.
- Backtest scoring reads the direction each rating claimed: a Sell that fell is a hit, and Hold reports no hit rate.
- The outcome window is configurable (`holding_period_days`), and reflection states the window it judges.
- A settled decision is not logged twice, and a failed reflection no longer stops the next run. (#645)
- The trader states entry and stop levels as prices, so a percentage no longer fails the whole proposal. (#1288)

### CLI

- `tradingagents backtest`, with `--run-id` to continue an interrupted sweep. (#1234)
- The previous run's selections come back as prompt defaults. (#1236, #920)
- A run with no readable rating says so; the live view no longer scrolls; messages that read like Python values are shown. (#649, #784)
- The state log keeps non-ASCII readable. (#1081)

### Data sources

- SEC EDGAR fundamentals vendor (US filers, keyless).
- Hong Kong and Shanghai tickers resolve to the symbols Yahoo serves. (#1342, #957, #1260)
- Reddit is fetched as one combined request per run. (#1286)
- One OHLCV cache file per symbol. (#1330)

### Models

- Current lineups for every provider: GPT-6 Astra and the GPT-5.6 family, Gemini 3.8 Flash, Claude Opus 5 and Fable 5.1, Grok 4.6, DeepSeek Flash, Qwen 3.8, GLM-5.3, MiniMax M3, Kimi K3 and the current Mistral snapshots.
- Every provider accepts a model ID the picker does not list.
- GLM traffic goes to the platform its key belongs to, and Ollama structured output no longer sends a tool_choice it rejects. (#1062)

### Changed

- The memory log records `REVIEW` for a decision with no readable rating, where it previously recorded `Hold`.
- Optional fields the model did not provide are named as such rather than omitted.
- Removed dependencies nothing imports: backtrader, redis, setuptools, langchain-experimental, parsel, tqdm. (#1353, #1070)

### Contributors

Thanks to everyone who reported these or sent a fix:

[@akashkpfreelancer](https://github.com/akashkpfreelancer), [@angziii](https://github.com/angziii), [@anupamme](https://github.com/anupamme), [@AyushKar2005](https://github.com/AyushKar2005), [@bulkypanda](https://github.com/bulkypanda), [@CadeYu](https://github.com/CadeYu), [@chiang21fcb](https://github.com/chiang21fcb), [@dajiaohuang](https://github.com/dajiaohuang), [@dewrama](https://github.com/dewrama), [@DogInfantry](https://github.com/DogInfantry), [@emitov](https://github.com/emitov), [@farukerdem34](https://github.com/farukerdem34), [@flydragon2018](https://github.com/flydragon2018), [@fusshell](https://github.com/fusshell), [@Ganesh1729-ui](https://github.com/Ganesh1729-ui), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@hamzabudeir](https://github.com/hamzabudeir), [@ihsieh31](https://github.com/ihsieh31), [@jaylew20250206](https://github.com/jaylew20250206), [@kaushik-yadav](https://github.com/kaushik-yadav), [@kbnnf](https://github.com/kbnnf), [@kevinkda](https://github.com/kevinkda), [@LudwigJMarx](https://github.com/LudwigJMarx), [@lx7720](https://github.com/lx7720), [@malandrindev](https://github.com/malandrindev), [@mhd325ic-hash](https://github.com/mhd325ic-hash), [@minhdn90](https://github.com/minhdn90), [@miznan](https://github.com/miznan), [@mmssix](https://github.com/mmssix), [@mrbob-git](https://github.com/mrbob-git), [@newnewself](https://github.com/newnewself), [@prithvirajrh](https://github.com/prithvirajrh), [@PyriteResearch](https://github.com/PyriteResearch), [@Rajatendu1](https://github.com/Rajatendu1), [@Recnelis0](https://github.com/Recnelis0), [@Rodvask](https://github.com/Rodvask), [@samhoooo](https://github.com/samhoooo), [@sheiun-xu](https://github.com/sheiun-xu), [@shivsin25](https://github.com/shivsin25), [@SmileShaun](https://github.com/SmileShaun), [@SonnyRajagopalan](https://github.com/SonnyRajagopalan), [@taro0915](https://github.com/taro0915), [@wupengbo125](https://github.com/wupengbo125), [@wxggzz](https://github.com/wxggzz), [@Yixiang-Wu](https://github.com/Yixiang-Wu), [@ZahirBodrike](https://github.com/ZahirBodrike), [@ZHUYAWEI](https://github.com/ZHUYAWEI), [@zkwang616](https://github.com/zkwang616).

## [0.4.0] — 2026-08-31

Look-ahead and point-in-time fixes across the data and memory layers, clearer
decision signals, working CLI checkpoint resume, and the GPT-5.6 / GLM-5.3 models.

### Fixed

- **FRED macro look-ahead.** Historical macro requests were served from today's
  data vintage, leaking later revisions into a backtest; both the observations
  and metadata requests now pin the vintage to the as-of date. (#1275)
- **Social sentiment look-ahead.** StockTwits and Reddit were fetched with no
  date, so a historical run showed today's chatter as if it were from the as-of
  date; the social path is now trimmed to the analysis window, via one shared
  UTC half-open window rule (`dataflows/date_window`) used by news too. (#1220)
- **Memory point-in-time guard.** `get_past_context` returned every resolved
  lesson regardless of the run date; each resolved entry now records the date
  its outcome became known, and a historical run only sees lessons resolved by
  the trade date. (#1251)
- **Premature reflection.** A decision was settled on a partial return if a rerun
  happened before its holding window fully traded; resolution now waits for the
  full window. (#1169)
- **Latest OHLCV bar dropped.** The newest bar with a NaN close was silently
  dropped before the date cutoff, making the previous trading day look like the
  latest; dates are normalized per element (DST- and non-US-market safe) and a
  missing latest close raises rather than falling back. (#1201)
- **Debate opening fabrication.** The first speaker in each debate round rebutted
  an empty opponent response, fabricating the other side; all five debators now
  open with their own case when no opponent has spoken. (#1176)
- **Silent Hold.** An unparseable Portfolio Manager rating (including a fullwidth
  colon) was coerced to a tradeable Hold; it now surfaces a `REVIEW` sentinel,
  with `parse_rating` keeping its silent default for compatibility callers. (#1170)
- **`--checkpoint` was a no-op on the CLI.** Checkpoint setup lived only in
  `propagate()`; the CLI streamed the checkpointer-less graph. The lifecycle is
  now shared, and a resume feeds `None` so LangGraph continues the interrupted
  run instead of duplicating messages. (#1249)
- **DeepSeek via OpenRouter.** `deepseek/<id>` fell through to default
  capabilities and had object-form `tool_choice` forced on it; the official
  namespace is stripped so it reuses the native DeepSeek quirks. (#1199)
- **Trader price grounding.** The Trader saw only the digested plan; it now also
  receives the technical market report so entry/stop levels anchor to real price
  structure. (#1167)

### Added

- **Configurable output-token cap.** `max_tokens` / `TRADINGAGENTS_MAX_TOKENS`,
  forwarded to every provider (Gemini as `max_output_tokens`), so a model that
  emits unbounded reasoning can be bounded instead of hanging. (#1204)
- **Latest models.** Added the GPT-5.6 family (`gpt-5.6` / `gpt-5.6-terra` /
  `gpt-5.6-luna`) and GLM-5.3 (`glm-5.3`, `glm-5.3-flash`). The default models
  are now `gpt-5.6` (deep) and `gpt-5.6-luna` (quick).

### Contributors

Thanks to everyone who reported these or sent a fix:

[@PyriteResearch](https://github.com/PyriteResearch), [@yiran1268](https://github.com/yiran1268), [@fabiolenine](https://github.com/fabiolenine), [@lx7720](https://github.com/lx7720), [@taro0915](https://github.com/taro0915), [@Jaswanth-Sriram-Veturi](https://github.com/Jaswanth-Sriram-Veturi), [@ariesy](https://github.com/ariesy), [@liangzj1999](https://github.com/liangzj1999), [@zkwang616](https://github.com/zkwang616), [@aniketshukla1](https://github.com/aniketshukla1), [@loulanyue](https://github.com/loulanyue), [@hudsonwa](https://github.com/hudsonwa), [@daleselaji-dev](https://github.com/daleselaji-dev), [@wolfoswald777-crypto](https://github.com/wolfoswald777-crypto).

## [0.3.1] — 2026-07-05

Correctness and stability patch: data look-ahead, graph-router crash-safety,
checkpoint identity, crypto sentiment sources, and configurable resilience.

### Fixed

- **Alpha Vantage look-ahead filter now runs.** The fundamentals payload is a
  JSON string, so the dict-only guard skipped filtering and future-dated reports
  leaked into historical runs; parse before filtering. (#1115, @zachthebird)
- **News analyst prompt matches the tool.** The prompt advertised
  `get_news(query, ...)` but the tool takes a ticker; aligned to stop
  hallucinated free-text query calls. (#1116, @shcheuk)
- **Shared debate/risk routers can't crash mid-run.** Both routers return more
  targets than any one edge mapped; every edge now shares the complete path map,
  so a fall-through under prompt/i18n/refactor drift stays routable.
  (#1088, @Fr3ya, @sa7an7, @Sushanth012)
- **Checkpoint resume respects graph shape.** The thread id folds in selected
  analysts, debate/risk depth, and asset mode, so a resume under different
  choices no longer continues the wrong graph. (#1089, @bossjoker1, @Ghraven)
- **Crypto sentiment sources resolve.** StockTwits lists crypto as `<BASE>.X`
  (Yahoo's `BTC-USD` 404s) and Reddit needs the base symbol to match; the social
  path now maps crypto correctly for both. (#1113, @suremadoreai)

### Added

- **Configurable LLM retry budget.** `llm_max_retries` /
  `TRADINGAGENTS_LLM_MAX_RETRIES` is forwarded to every provider, so a transient
  429 burst no longer aborts a run. (#1091, @yanggaome)
- **Bedrock API-key auth.** `AWS_BEARER_TOKEN_BEDROCK` authenticates Amazon
  Bedrock without AWS access keys and takes precedence over an ambient
  `AWS_PROFILE`. (#1103, @praxstack)
- **Latest Claude models.** Added Claude Sonnet 5 (`claude-sonnet-5`) and
  Fable 5 (`claude-fable-5`); effort control now covers the Claude 5 line.

## [0.3.0] — 2026-06-22

Stabilization and extensibility release: a CI gate, a unified verified
data-access contract, a provider and data-vendor registry, and a maintenance
sweep that hardened config precedence, the model catalog, data resilience, and
structured output.

### Added

- **CI gate.** GitHub Actions runs the pytest suite across Python 3.10-3.13,
  strict `ruff`, and a clean-install smoke that imports the package and CLI to
  catch undeclared dependencies. (#994, #197)
- **Provider registry.** OpenAI-compatible providers register as a single spec,
  and a generic `openai_compatible` endpoint covers vLLM, LM Studio, and relays.
  Adds NVIDIA NIM, Kimi, Groq, Mistral, and a native Amazon Bedrock client.
- **Macro and prediction-market vendors.** FRED macro indicators and Polymarket
  event probabilities, surfaced to the news and macro analysts.
- **Programmatic report output.** `TradingAgentsGraph.save_reports()` writes the
  same report tree the CLI produces, for headless and API runs. (#1037)
- **Env-configurable reasoning depth** via `TRADINGAGENTS_OPENAI_REASONING_EFFORT`,
  `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, and `TRADINGAGENTS_ANTHROPIC_EFFORT`,
  each gated to the models that accept it.

### Changed

- **Verified data-access contract.** Symbol normalization on every vendor path
  (identity, returns, CLI, news); the configured vendor list is the exact
  resolution chain with no silent fallback to unselected vendors; a typed
  `VendorError` taxonomy; look-ahead-safe news windows; stale-OHLCV rejection;
  inclusive yfinance date ranges.
- **Config precedence.** An explicit `TRADINGAGENTS_*` value or CLI flag now wins
  over interactive defaults for debate and risk round counts,
  `--checkpoint / --no-checkpoint`, and the Docker provider profile; invalid
  boolean env values fail loudly. (#975, #976, #977)
- **Current-generation model catalog.** Refreshed provider lineups; retired
  `gpt-4.1`, Claude Sonnet 4.5, and the Gemini 2.5 line.
- **Optional vendors degrade** instead of aborting a run: a failed macro or
  prediction-market lookup returns a no-data sentinel.
- **Analyst prompts lead with the current date** so tool-call date ranges anchor
  to the run date rather than the model's training cutoff. (#836)

### Fixed

- **Instrument identity.** Deterministic ticker-to-company resolution prevents
  wrong-company hallucination, and a verified market-data snapshot grounds price
  and indicator claims. (#814, #830)
- **Social and market data sources.** Reddit RSS-first with 429 backoff,
  StockTwits transport hardening, and Alpha Vantage timeout plus
  key-versus-rate-limit handling.
- **Structured output.** Local OpenAI-compatible servers no longer reject
  object-form `tool_choice`; a thinking model that returns no parsed result falls
  back to free text; null-ish strings in optional price fields coerce to `None`.
  (#1038, #1051, #1057)

### Removed

- The no-op `analyst_concurrency_limit` config knob; parallel analyst execution
  is planned for a later release. (#979)
- The unused committed `uv.lock`. (#1030)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

[@CadeYu](https://github.com/CadeYu), [@Zavianx](https://github.com/Zavianx), [@weijianz-opc](https://github.com/weijianz-opc), [@naltun](https://github.com/naltun), [@brahmasky](https://github.com/brahmasky), [@nik2208](https://github.com/nik2208), [@thieucong98](https://github.com/thieucong98), [@Derekko-web](https://github.com/Derekko-web), [@LukiPrince](https://github.com/LukiPrince), [@Eddieargenal](https://github.com/Eddieargenal), [@Ghraven](https://github.com/Ghraven), [@ms32035](https://github.com/ms32035), [@yting27](https://github.com/yting27), [@nyxst4ck](https://github.com/nyxst4ck), [@KenCheung-AIxFinance](https://github.com/KenCheung-AIxFinance), [@yangyusheng2n](https://github.com/yangyusheng2n), [@fareloj](https://github.com/fareloj), [@haosenwang1018](https://github.com/haosenwang1018), [@octo-patch](https://github.com/octo-patch), [@seifenk](https://github.com/seifenk), [@CaoYuhaoCarl](https://github.com/CaoYuhaoCarl), [@mihailnica10](https://github.com/mihailnica10), [@Dado-hash](https://github.com/Dado-hash), [@Handsomemikezzz](https://github.com/Handsomemikezzz), [@ydhawesome](https://github.com/ydhawesome), [@macd2](https://github.com/macd2), [@AyushKar2005](https://github.com/AyushKar2005), [@wildhuman](https://github.com/wildhuman), [@robert23kim](https://github.com/robert23kim), [@bngness](https://github.com/bngness), [@tedix-rodrigo](https://github.com/tedix-rodrigo), [@malaccan](https://github.com/malaccan), [@rfalken78](https://github.com/rfalken78), [@dengli1971-droid](https://github.com/dengli1971-droid), [@proofconcept39](https://github.com/proofconcept39), [@prasta1](https://github.com/prasta1), [@liximin](https://github.com/liximin), [@jeffhuen](https://github.com/jeffhuen), [@mazar](https://github.com/mazar), [@soyangelromero](https://github.com/soyangelromero), [@CNQQC](https://github.com/CNQQC), [@dovetaill](https://github.com/dovetaill), [@fperdigon](https://github.com/fperdigon), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@RSXLX](https://github.com/RSXLX).

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
