# Intraday (day-trader) mode

An intraday run plans one trading session instead of rating a position over the
holding period. It adds the Day Trader analyst, which reads 5-minute bars for the
session's levels and paper position sizes; the Trader and the Portfolio Manager
then write a plan for that session: direction, entry, stop, target, exit-by time,
size and what invalidates it. A later run scores the plan on that session's bars.
Facts below were checked on 2026-10-29 against the sources named; broker times
and rates change, so re-check them before relying on them.

> Paper trading and research only. A plan is not an order: nothing is sent to a
> broker, no brokerage credentials are read, and the reports say so. Prices come
> from Yahoo Finance through an unofficial library, and the report is not advice
> from a SEBI-registered research analyst or investment adviser.

## Running

```bash
tradingagents --horizon intraday --exchange nse --ticker RELIANCE --analysts market,news
tradingagents --horizon intraday --ticker AAPL --analysts market --save --no-show
tradingagents --horizon intraday --exchange nse --ticker TCS --date 2026-10-28   # plans the next session after 28 Oct
```

- `--horizon swing|intraday` sets the run; without it
  `TRADINGAGENTS_DECISION_HORIZON` (default `swing`) does. Swing runs are
  unchanged.
- The analysis date defaults to today, with no question: your machine's date, or
  the listing's when that is earlier (New York's for a US ticker on a machine in
  India after midnight, while the US session may still trade). `--date` names a
  past date, and the plan is then for the next trading session after it.
- The Day Trader (`day_trader`) is added when it is not among the analysts
  ("✓ Day Trader added"). In a swing run it can also be chosen like any analyst.
- Crypto is refused: it trades around the clock, and its sessions are not modelled.
- A backtest refuses the intraday horizon: it scores swing ratings over the
  holding period and does not replay intraday plans.
- Without a terminal, the run needs the same settings as any headless run (README,
  "CLI Usage"): `--ticker`, `--analysts`, `--save` or `--no-save`, `--show` or
  `--no-show`, and the `TRADINGAGENTS_*` provider, model, language and round
  variables; `--date` is not needed.

From Python:

```python
config = DEFAULT_CONFIG.copy()
config["decision_horizon"] = "intraday"
ta = TradingAgentsGraph(["market", "news"], config=config)   # day_trader is added
state, decision = ta.propagate("RELIANCE.NS", "2026-10-28")
print(state["intraday_plan"])                                  # the plan, as data
```

### The /day-trader skill (Claude Code)

The repository carries a Claude Code skill, `.claude/skills/day-trader/`. In
Claude Code, opened in this folder, type `/day-trader RELIANCE` or
`/day-trader TCS bse` (the exchange defaults to nse). It runs one headless
intraday analysis through `scripts/day-trader.sh`:

```bash
--horizon intraday --exchange <nse|bse> --ticker <TICKER> --analysts day_trader,market,news --save --no-show --html
```

with input from `/dev/null`, then summarises the saved report: the target
session and data time, bias, entry, stop, target, size, exit-by, invalidation,
key levels, charges and what was unavailable, quoting only the report's numbers.

- It runs only when you type it (`disable-model-invocation: true`), and it
  pre-approves only its own script, never `tradingagents login` or `logout`.
- Set the headless settings in `.env` in this folder: `TRADINGAGENTS_LLM_PROVIDER`,
  `TRADINGAGENTS_QUICK_THINK_LLM` or `TRADINGAGENTS_DEEP_THINK_LLM`,
  `TRADINGAGENTS_OUTPUT_LANGUAGE`, `TRADINGAGENTS_MAX_DEBATE_ROUNDS`,
  `TRADINGAGENTS_MAX_RISK_ROUNDS`, and the provider's API key.
- It runs `$TRADINGAGENTS_PYTHON -m cli.main` when `TRADINGAGENTS_PYTHON` is set
  (for example to this repository's `.venv/bin/python`), else the `tradingagents`
  command on your PATH, else `python3 -m cli.main`. Export `TRADINGAGENTS_PYTHON`
  before starting Claude Code: the script reads it from its environment, not
  from `.env`. An older `tradingagents` on your PATH fails with "No such option:
  --horizon"; set `TRADINGAGENTS_PYTHON` then.
- A run takes several minutes (often 5 to 15), which can exceed the 10-minute
  limit of Claude Code's Bash tool; the skill asks for the longest timeout, or a
  background run, and never retries on its own.
- Committing `.claude/skills/` shares the skill with everyone who opens the
  repository in Claude Code.

## Cut-off and the session a plan is for

**What the run can see.** A run dated in the past sees bars through that day's
session close and nothing later. A run dated today sees the bars that have ended
by now: Yahoo labels a bar by its start, so a bar counts only once its start plus
the interval has passed, and the bar still forming is left out. Bars are the
regular session only (for NSE/BSE, special sessions included), in the exchange's
time zone; rows without a price, or with a price of 0 (a bad Yahoo print), are
dropped, and rows with volume 0 are kept
(Yahoo has no pre-open trades, and its 09:15 1m NSE bar showed volume 0 on 28
and 29 Oct 2026).

**The session the plan is for** (`target_session`):

| The run | The plan is for |
|---|---|
| Dated in the past (D) | the next trading day after D |
| Today, before the open | today's session |
| Today, while the session is open | the rest of today's session |
| Today, after the close, or on a non-trading day | the next trading day |

NSE and BSE follow their calendars (`tradingagents/markets/exchanges.py`,
holidays and special sessions included); other listings count weekdays in their
bars' time zone, with the hours their last five sessions' bars show, however few
sessions the snapshot is asked for. Between the square-off time and the close, a
run dated today still plans the rest of today's session, and the snapshot notes
that the square-off has passed, so no new intraday position can be opened. The
report header names the session and the exit-by time, for example "Horizon:
intraday paper plan for <day> (<why>); exit by 15:10 IST".

## The Day Trader's snapshot

`get_intraday_snapshot` (bars of 1m, 5m or 15m, over 1 to 10 recent sessions,
5m and 5 by default) gives the Day Trader, as markdown tables:

- the session the plan is for, why, and the exit-by (square-off) time;
- the last complete session: open, high, low, close;
- the plan's session so far, once it has traded: open, high, low, last, VWAP
  (typical price), volume, the gap from the previous close, the 15- and
  30-minute opening ranges, relative volume (volume so far against the average
  of the earlier sessions in the bars by the same time of day, leaving out a
  special session on other hours, such as Muhurat trading), and last versus
  VWAP;
- classic floor pivots (P, R1-R3, S1-S3) from the last complete session;
- ATR(14) on the bars and on daily bars;
- paper sizing examples: a long and a short at the newest close, with stops 1x
  and 2x the bars' ATR away;
- the latest 12 bars, and notes: the exchange hours in force on the date, the
  feed delay, bars left out, and the capital used.

Every number names the bars it came from, and the snapshot is the source of
truth for intraday prices, levels and sizes. The Day Trader also reads the
verified market snapshot and the market context (benchmark, sector index, and for
NSE/BSE India VIX, USD/INR and Brent).

**Paper sizing.** The share count is the largest whose loss at the stop plus the
round-trip charges stays within the risk amount, `intraday_risk_pct` (0.5%) of
the capital: floor(risk / (|entry - stop| + charges per share)). It is then capped
at floor(capital x `intraday_leverage_cap` / entry). The example reports the
exposure, the margin needed at that leverage, the money at risk and the estimated
round-trip charges (the configured ones, [Charges](#charges)). The capital is `intraday_paper_capital` (100,000 in the
listing's currency, a paper notional, not an account balance). A `--portfolio`
sets it to the book's value (long holdings at their last close plus cash) only
when the whole book is in the listing's currency; the snapshot says which was used.

## The plan

The Portfolio Manager's decision carries the plan as labelled lines: **Direction**
(long, short or none), **Entry Price**, **Stop Loss**, **Target Price**, **Exit
By** (HH:MM), **Position Size (shares)** and **Invalidation**. They stay in
English in a run in another language, and so do their values (long, short or
none; plain numbers; HH:MM on a 24-hour clock), since they are read back.

- The direction follows the rating: Buy or Overweight is a long, Underweight or
  Sell a short (intraday only) or no trade, Hold no trade. The Portfolio Manager
  is told to stand aside on a session the reports flag as one not to trade: a
  results or other event day, very low relative volume, a gap near the price
  band, or a square-off already passed.
- For NSE/BSE the exit-by is the earlier of the plan's and the square-off, and the
  square-off when the plan gives none; a later one is moved, with a note.
- An exit-by that leaves the plan no bar to trade, one not after the session's
  open (the calendar's for NSE/BSE, the bars' usual first bar elsewhere) or not
  after the moment a plan made during its session was made, is replaced by the
  square-off, or the session close, with a note. "3:55" meant as 3:55 pm reads
  as 03:55.
- A direction the rating does not call for, or more shares than the paper sizing
  allows for that stop, is noted, not corrected. So is a long or short with no
  entry, or with its stop or target on the wrong side of the entry: it is scored
  as no trade.
- The decision the reports and the /day-trader skill show is the plan as it is
  stored and scored (the moved exit-by, for example), with a **Plan Checks**
  line listing the notes. A decision the model wrote as free text keeps its own
  lines, with the Plan Checks line added after them. Its values are read past a
  remark in brackets ("96.60 (2R)", "Short (intraday only)"); a line meant as a
  value that still cannot be read ("about 96.60", "15h10", a range) is left
  out, with a note.
- Prices written as "₹1,405.50", "Rs 1405" or "INR 1405" are read.
- The plan is kept in the state as `intraday_plan`, with the session, the
  reference price (the newest complete 5m close when the run started) and the
  notes. The Day Trader's report is saved as `1_analysts/day_trader.md` and the
  decision as `5_portfolio/decision.md`; the report's disclaimer adds that an
  intraday plan is a paper exercise.

## Scoring the plan

The plan goes into the memory log under `HORIZON: intraday`, as an
`INTRADAY PLAN: <json>` line. A later run's Memory Log step (or
`ta.settle_all_pending()`) scores it once the session's close is final: for
NSE/BSE after 16:00 IST on that day, elsewhere once the session's date is over in
UTC and Yahoo's bars for it are final. It is replayed on that session's 5m bars,
never scored by the holding-period return:

- **Order type** from the reference price: a long's entry at or above it is a buy
  stop (a breakout), below it a buy limit (a pullback); a short's entry at or
  below it is a sell stop, above it a sell limit. With no reference price, the
  session's first open stands in.
- **Fill** when a bar reaches the entry; a bar that opens beyond it fills at its
  open. A plan made in a run dated today, during its own session, counts only
  bars that start at or after the moment it was made (none when it was made
  after the session). A run dated in the past plans as of that date's close, so
  its plan counts the whole session, whenever the run is made.
- **The fill bar** is checked for the stop only, since its high and low may come
  after the fill. A fill at an open already beyond the target (a gap through both)
  exits there at once.
- **Later bars**, one at a time: a bar opening beyond the stop or the target exits
  at its open, since the open comes first; otherwise a bar reaching the stop exits
  at the stop, even when it also reaches the target (the stop counts first), and a
  bar reaching the target exits at the target.
- **Exit-by:** the position closes at the open of the bar the exit-by time falls
  in, the first bar ending after it, and no entry fills from then on: the 15:10
  bar for an exit-by of 15:10, and for 15:12 too, since the rest of that bar
  comes after the square-off. Otherwise it closes at the last bar's close.
- **Charges:** the configured intraday charges for NSE/BSE (`india_equity_intraday`
  by default, below), none elsewhere. With no share count the plan is scored on
  one share.

The outcome (exit reason: target, stop, exit_by, session_close, not_triggered,
no_plan or no_data; fill and exit; gross and net P&L, charges, net return and R
multiple) is written as `INTRADAY OUTCOME: <json>`, the tag becomes
`[date | ticker | rating | <net %> | n/a | intraday | resolved:<session>]`, and
the model writes a short reflection. A session older than Yahoo's 5m window is
recorded as "Not scored: outside Yahoo's intraday window", and a session with no
bars as not scored, both without a model call; so let a later run, or
`settle_all_pending()`, settle a plan within about 60 days. Backtest summaries
leave intraday entries out.

## India: square-off, charges and leverage

### Broker MIS square-off times

Brokers close intraday (MIS) positions themselves near the end of the session.
TradingAgents applies one time to every NSE/BSE listing, `intraday_square_off`
(default 15:10, Upstox's and Angel One's for stocks with derivatives; Groww's is
earlier), caps every plan's exit-by at it, and uses none elsewhere (other
listings exit by the session close). Times for equity MIS, as the brokers' pages
read on 29 Oct 2026:

| Broker | Stocks with derivatives (closing-auction stocks) | Other stocks | Source |
|---|---|---|---|
| Upstox | 15:10 from 11 Sep 2026 (15:05 before) | 15:25 (was 15:20) | upstox.com announcement, 4 Sep 2026 |
| Angel One | 15:10 for cash MIS (was 15:15); bracket orders 15:05 | not stated | angelone.in knowledge-centre article on the closing auction, 30 Jul 2026, updated 27 Aug 2026 (its older support page still shows 15:15 / 15:20) |
| Zerodha | 15:12 (15:10 at launch; changed 10 Aug 2026) | 15:25 | support.zerodha.com intraday auto square-off timings; Z-Connect product updates, 10 Aug 2026 |
| ICICI Direct | 15:12 | 15:25 | icicidirect.com FAQ pages (undated) |
| Groww | about 15:05: new intraday orders blocked after 15:05 | 15:20 | groww.in risk policy v8.1.4 (undated) and blog, 29 Jul 2026 (its help pages still say 15:20 for all) |

- Zerodha charges ₹50 plus GST for each order it squares off, and ICICI Direct
  the same on its iVALUE-type plans.
- For stocks without derivatives brokers square off later (15:20-15:25), so 15:10
  is early for them; set `intraday_square_off` to your broker's time.
- From 7 Dec 2026 stocks with derivatives trade continuously until 15:30 (SEBI
  circular of 15 Oct 2026). No broker had announced new times by 29 Oct 2026;
  re-check then.

The session rules themselves (pre-open, the closing auction from 3 Aug 2026 and
its move on 7 Dec 2026, the close final at 16:00) are in
[docs/india.md](india.md#indian-market-rules-the-agents-use); the snapshot states
the hours in force on the analysis date.

### Charges

Sizing and scoring use the `india_equity_intraday` profile by default
(`tradingagents/markets/costs.py`, as of 2026-10-29):

| Charge | Rate | Source |
|---|---|---|
| Brokerage | the lower of 0.03% and ₹20 per order | a discount broker (Zerodha) |
| STT | 0.025% on the sell side | NSE/FATAX/73524 |
| Exchange charge | NSE 0.00307%, BSE 0.00375%, each side | NSE/FA/73061; a broker page for BSE |
| SEBI fee | ₹10 per crore | NSE/FA/40632 |
| Stamp duty | 0.003% on the buy side | Indian Stamp Act Sch. I |
| GST | 18% on brokerage, exchange and SEBI charges | |

A ₹1 lakh round trip on NSE costs ₹82.68 under this profile (0.083%): brokerage
₹40, STT ₹25, exchange ₹6.14, SEBI ₹0.20, stamp duty ₹3 and GST ₹8.34. These
rates are a dated example, not what your broker charges. Set your own with
`intraday_cost_overrides`, which takes any field of the profile, for example
`{"brokerage_rate": 0.0, "brokerage_cap": None}` for a broker charging no
intraday brokerage. Name another profile with `intraday_cost_profile`
(`TRADINGAGENTS_INTRADAY_COST_PROFILE`), or `none` for no charges. The snapshot's
sizing, the plan's size check and the later scoring all use these settings. A
mistyped profile or field stops the run at its start. The swing settings
`cost_profile` and `cost_overrides` do not apply here, and other listings carry
no charges.

### Leverage and margin

SEBI requires brokers to collect VaR and ELM margins upfront in the cash segment
(circular of 19 Nov 2019); collecting at least 20% upfront spares them the
short-collection penalty (circular of 31 Jul 2020), and both stand in SEBI's
Master Circular of 30 Dec 2024. In practice brokers offer at most 5x on equity
MIS (20% margin), less for volatile stocks; Zerodha states "Equity 5X (20%
of the trade value based on the volatility of the stock (VaR+ELM+Adhoc))". The
20% is a penalty safe harbour, not a written 5x cap.

`intraday_leverage_cap` defaults to 1.0, no leverage: a paper position is never
larger than the capital. Raise it only knowingly; the sizing then reports the
margin the position would tie up.

## Yahoo's intraday data

- **How far back.** 1m bars for the last 7 days, 5m and 15m for the last 60,
  counting today. An older date gets "Yahoo serves 5m bars for about the last 60
  days; <date> is outside that window (unavailable, not an absence of trading)",
  and no request is made. (On 2026-10-29 Yahoo's chart API refused 1m requests
  longer than 8 days and 5m windows older than about 60 days.)
- **No 30m bars.** yfinance builds them from 15m bars on clock half-hours, so
  NSE's 09:15 start would leave the first bar 15 minutes long.
- **NSE and BSE bars** are in Asia/Kolkata, 09:15-15:30, labelled by their start,
  with no pre-open bars. Yahoo lists NSE quotes as real-time and BSE quotes as 15
  minutes delayed (help.yahoo.com/kb/SLN2310; not measured), so BSE bars ending
  within 15 minutes of the download are left out.
- **The closing auction.** For a stock with derivatives, from 3 Aug 2026, Yahoo
  shows no bars from 15:16 until one large bar at the auction's random close,
  which carries the official close (RELIANCE.NS: 15:28 on 28 Oct, 15:29 on
  29 Oct 2026); on 5m bars it falls in the 15:25 bar. Post-close trades do not
  appear. From 7 Dec 2026 the auction print (15:38-15:40) may fall after Yahoo's
  15:30 session end and be missing: a prediction to re-test then.
- **Volumes and prices.** On 29 Oct 2026 an NSE stock's daily volume on Yahoo
  exceeded the sum of its 1m bars by about 2.3%, which fits the pre-open and
  post-close trades being left out of the bars (an inference).
  Bars are not dividend-adjusted, and the official open and close can differ
  slightly from the first and last bar.
- **Cache.** One file per symbol and interval under
  `<data_cache_dir>/intraday/` (default `~/.tradingagents/cache/intraday/`),
  holding the whole window. A file serves only the exchange-local day it was
  written; a run whose cut-off is in a session still trading refetches once the
  file is older than `intraday_cache_ttl_seconds` (60). No bar ending after the
  download is served, and empty downloads are never cached.

## Settings

| Variable | Key | Default | Checked at start-up |
|---|---|---|---|
| `TRADINGAGENTS_DECISION_HORIZON` | `decision_horizon` | `swing` | swing or intraday, any case |
| `TRADINGAGENTS_INTRADAY_SQUARE_OFF` | `intraday_square_off` | `15:10` | a time as HH:MM (IST for NSE/BSE) |
| `TRADINGAGENTS_INTRADAY_CAPITAL` | `intraday_paper_capital` | `100000.0` | a positive amount |
| `TRADINGAGENTS_INTRADAY_RISK_PCT` | `intraday_risk_pct` | `0.5` | above 0 and at most 100 (percent) |
| `TRADINGAGENTS_INTRADAY_LEVERAGE_CAP` | `intraday_leverage_cap` | `1.0` | a positive multiple |
| `TRADINGAGENTS_INTRADAY_COST_PROFILE` | `intraday_cost_profile` | `india_equity_intraday` | a profile in `tradingagents/markets/costs.py`, any case |
| (config only) | `intraday_cost_overrides` | `{}` | known fields of the profile (checked when the graph is built) |
| (config only) | `intraday_cache_ttl_seconds` | `60` | |
| `TRADINGAGENTS_PYTHON` | (the /day-trader script only) | unset | |

A value that fails its check stops the run at start-up, for example "Invalid
value for TRADINGAGENTS_DECISION_HORIZON: expected swing or intraday, got 'day'".
`--vendor intraday_data=yfinance` names the data vendor (the only one).

## Limits

- **One log entry per ticker per date**, across both horizons: a second run on
  the same ticker and date, intraday or swing, is not logged.
- **Two clocks.** The run's default date is the earlier of your machine's date
  and the listing's, while the session is judged on the exchange's clock. On a
  machine in the US evening India's date is already the next day, so a run dated
  the US date can plan the Indian session already under way; only bars after the
  decision count then.
- **Resuming** a checkpointed run reuses the session and reference price saved
  when it started, which is stale for a run dated today resumed hours later.
- **The researchers and risk analysts** keep their swing framing, apart from the
  Day Trader's report; the Trader and the Portfolio Manager carry the session.
- **Listings without a calendar** (anything but NSE/BSE) count weekdays, so a US
  market holiday counts as a session (its plan then scores as no data); they
  settle up to a day after their close, and 24-hour markets are not modelled.
- **Free-text decisions.** When the model writes no structured output, the plan
  is read from its labelled lines, and a line that cannot be read is left out
  with a note. Labels it translates anyway, or a table in place of the lines,
  leave the plan empty, scored as no trade, with a note.
