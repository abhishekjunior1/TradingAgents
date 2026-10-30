# AlphArena paper trading (optional)

[AlphArena](https://alpharena.io) is a public arena where trading agents compete
with simulated money on US stocks and ETFs. With it turned on, the CLI can send a
finished run's decision there as a paper trade, after showing it to you and
asking. It is off by default, and nothing in a run changes with it off. Facts
below come from AlphArena's documentation, terms and privacy policy as saved on
2026-10-29. Of its API, only the public leaderboard has been called from
TradingAgents (on 2026-10-29); the trade endpoints have not, so try them in
sandbox mode first.

> Simulated money only. AlphArena is paper trading ($10,000 of simulated capital
> per agent); TradingAgents has no broker code and places no real order. A
> paper-trading record is not advice. Indian listings are never sent (see
> [India is excluded](#india-is-excluded)).

## What AlphArena is

- Paper trading only, in US stocks and ETFs (it also lists options, whose P&L it
  says is not independently verified; TradingAgents never sends one). Its terms
  have an "AI Agent" account type "for autonomous bots submitting signals
  programmatically via the REST API".
- Orders are market-style, filled at AlphArena's own price from market data. The
  entry price you send is a reference it checks against its mark, within a band
  whose width is not documented. There are no limit orders and no partial closes.
- A position may be at most 25% of the book. A trade of $2,000 or more needs a
  public thesis of at least 8 characters, and a thesis once sent cannot be edited.
- Limits: 60 requests a minute, and 10 signals a day on the free tier.
- Keys: a sandbox key (`aa_test_...`) works at once; a live key (`aa_live_...`)
  needs your email verified. AlphArena stores keys hashed and cannot show them
  again.

## Risks

- **The trade endpoints have not been seen working.** Only the public
  leaderboard has been called; the other endpoints, fields and error codes come
  from AlphArena's documentation. Test in sandbox mode first.
- **No legal entity is named**: only "AlphArena", Delaware law, and legal@ and
  privacy@ addresses. The terms can change with 14 days' email notice, and the
  service can be discontinued at any time.
- **Rankings mean little yet.** On 2026-10-29 its home page showed 28 agents and
  17 open trades, with 5 agents ranked; by default an agent needs 5 trades to be
  ranked.
- **It discloses gaps in its own leaderboard's integrity**: imported track
  records, trades corrected after entry, and an `opened_at` callers may supply
  (TradingAgents never sends one).
- **Sandbox trades are probably not ranked**: the terms call live keys the ones
  "used for leaderboard-eligible trading".
- **A broad licence**: AlphArena gets a perpetual, irrevocable licence to display
  your signals and strategy text. After you delete the account, your signals stay
  on the leaderboard in anonymized form.
- **What it logs**: your IP address and user agent; API logs are kept for 90 days,
  and its website runs Google Analytics. It does not ask for brokerage
  credentials.
- **Public text is untrusted.** Other agents' names and theses are other people's
  text: TradingAgents prints them escaped and never puts them into a prompt.
- **Regulation.** The SEBI positions below concern Indian listed securities; paper
  trading US tickers from India is not addressed by them. This is not legal advice.

## Register (by hand, once)

TradingAgents does not register you or accept terms for you.

1. Read AlphArena's Terms and Privacy Policy. You must be 18 or older, and under
   the terms you, the operator, are responsible for everything your agent does.
2. Register on alpharena.io (its `/register` page, or the registration call its
   API documentation describes, made yourself). Give:
   - a pseudonymous agent name: the terms forbid using another entity's name
     without authorization, so do not call it "TradingAgents" or "Tauric";
   - an email address (kept private);
   - a short public description and strategy summary that reveal nothing about
     you or your holdings.
3. Keep both keys in a password manager. Start with the sandbox key:
   `ALPHARENA_API_KEY=aa_test_...`. To trade live, click the verification link
   AlphArena emails you within 24 hours, then use the `aa_live_` key with
   `TRADINGAGENTS_ARENA_MODE=live`.
4. Set `TRADINGAGENTS_ARENA_AGENT_ID` to your agent id, for `tradingagents arena status`.

## Turn it on

```bash
# .env in the repository folder, or exported
ALPHARENA_API_KEY=aa_test_...         # read only from the environment, at each call
TRADINGAGENTS_ARENA_MODE=sandbox      # sandbox (aa_test_ keys) or live (aa_live_ keys)
TRADINGAGENTS_ARENA_AGENT_ID=your-agent-id
TRADINGAGENTS_ARENA=1                 # or pass --arena for one run
```

```bash
tradingagents --ticker AAPL --analysts market,news --arena     # asks before sending
# Without a terminal --date names the run's date: the earlier of this machine's and New York's.
day=$(printf '%s\n' "$(date +%F)" "$(TZ=America/New_York date +%F)" | sort | head -n 1)
tradingagents --ticker SPY --date "$day" --analysts market --save --no-show \
  --arena --arena-yes                     # no terminal needed; sends without asking
```

After the run (once the live view has closed) the CLI shows the paper trade and
asks "Send this paper trade to AlphArena (simulated money)? [y/N]"; the answer
defaults to no, and Ctrl+C counts as no. It is offered only when all of these
hold, each checked before any request:

- `arena_enabled` is on (`TRADINGAGENTS_ARENA`, or `--arena`; `--no-arena` turns
  it off for one run);
- the ticker is a plain US stock or ETF ticker of 1-5 letters: no exchange
  suffix, index, crypto pair, currency pair, future or share-class ticker such as
  BRK-B (whose spelling AlphArena does not document);
- the decision is a swing rating, not an intraday plan;
- the run is dated today in New York, since AlphArena fills at today's price.
  The CLI's default date for a US ticker is the earlier of the machine's date and
  New York's, and the CLI takes no date after the machine's. On a machine set to
  India time, from midnight until 09:30 IST (10:30 IST while New York is on
  standard time) the machine's date is a day ahead of New York's: the default is
  then New York's, and a run dated it is a current run, not a past one, so it can
  be sent; a run dated the machine's date is refused. West of New York it is the
  other way round late in the evening (from 21:00 in California, 23:00 in
  Chicago): New York's date is after the machine's, so no run can be sent until
  the machine's own midnight. A run without a terminal names the earlier date
  with `--date`, as above;
- there is a terminal to ask at, or `--arena-yes`. Without either, nothing is
  sent and the key is not read.

The same checks run before the analysis starts. When the decision could not be
sent, the CLI says why and the analysis goes on. When it could, the CLI checks,
without a request, that `ALPHARENA_API_KEY` is set and is a key for `arena_mode`
(`aa_test_` in sandbox mode, `aa_live_` in live); if not, it stops before anything
is analysed, with exit code 2: set the key, or rerun with `--no-arena`. The key
is never shown. `--arena-yes` does not turn the arena on: with the arena off, the
CLI says before the analysis that it will send nothing, and the analysis goes on.
After the run everything is checked again.

Backtests never send anything, and neither does `TradingAgentsGraph.propagate()`:
the offer is part of the CLI only. From Python, call
`tradingagents.arena.propose_plan` and `send_plan` yourself; `propose_plan`
refuses what the CLI refuses, an intraday run included (it reads the run's
horizon from its state). The `/day-trader` skill never reaches it: its runs
are intraday, on NSE/BSE, and without a terminal.

## What a decision becomes

| Rating | Paper trade |
|---|---|
| Buy | Open a long of `arena_buy_position_usd` ($1,950), closing any open short first |
| Overweight | Open a long of `arena_overweight_position_usd` ($1,000), closing any open short first |
| Hold | Nothing |
| Underweight | Close the open longs in the ticker |
| Sell | Close the open longs; with `arena_allow_short`, also open a short of `arena_buy_position_usd` |

- Nothing is opened in a direction that already has an open trade in the ticker,
  or after a close in the same plan failed.
- Nothing is opened without a reference price: when Yahoo cannot give the latest
  close (rate limited, say), the plan still closes what the rating closes, and a
  note says why.
- $1,950 stays under the $2,000 from which AlphArena requires a public thesis. A
  size of $2,000 or more without `arena_send_thesis` is not sent, with a note.
- The same decision is sent once: its idempotency key is the SHA-256 of
  `ticker|trade date|rating|horizon`, and an open the ledger shows AlphArena
  accepted in the same mode is skipped.

### What is sent, and what never is

- **An open:** the ticker, the direction, the entry price (the latest close from
  Yahoo daily bars, as a reference), the position value in dollars and the
  idempotency key; the Portfolio Manager's price target and the Trader's stop
  loss, each only when it lies on the right side of the entry; the confidence
  (low 0.4, medium 0.6, high 0.8). These come from the agents' structured
  output, so a "Price Target" or "Stop Loss" line quoted in the thesis or the
  Trader's reasoning is never sent in their place; a decision a model wrote as
  free text is read from its labelled lines. A thesis is sent only with
  `arena_send_thesis`: the executive summary, cut to 300 characters, public and
  locked once sent. With `--portfolio` that summary can mention your holdings,
  so read it when asked.
- **A close:** the trade id and a reason, "<Rating> rating" or "manual", which
  AlphArena may show publicly.
- **Every request:** the user agent `TradingAgents/<version> arena-client`; the
  key (`Authorization: Bearer`) only on your own trades' endpoints, never on the
  public profile, P&L, portfolio and leaderboard pages.
- **Never:** `opened_at`, options, tags or strategy names, analyst reports,
  debate text, prompts, your portfolio data, LLM keys, or any NSE/BSE symbol.

The address is fixed in the code (`https://alpharena.io/api/v1`): no setting
changes it, and redirects are not followed, so the key cannot be sent elsewhere.
A sandbox key in live mode, or a live key in sandbox mode, is refused before any
request. The key is never logged, and is replaced with `***` in any error.

## The ledger and the report

Every open and close sent to AlphArena goes into a local ledger, one JSON line
each: `<results_dir>/arena/ledger.jsonl` (by default
`~/.tradingagents/logs/arena/ledger.jsonl`), or `arena_ledger_path`. A line holds
the time (UTC), mode, ticker, trade date, rating, action, idempotency key, the
payload, and AlphArena's answer or the error; never the key. Nothing is sent when
the ledger cannot be written.

A saved report notes what AlphArena accepted, in its header: "Paper trade sent
to AlphArena (simulated money, sandbox mode): opened a long in AAPL ($1,950,
trade ...)", and its Mode line then reads "no real orders are placed". Without a
submission the report is unchanged. What AlphArena answers never reaches the
state log, the memory log or a prompt.

## Commands

```bash
tradingagents arena status [--period all|7d|30d|90d]            # your agent's public profile, P&L, positions; no key
tradingagents arena leaderboard [--period 30d] [--sort return|sharpe|consistency] [--limit 20]
tradingagents arena trades [--status open|closed|all]            # your trades; needs the key
tradingagents arena close AAPL [--yes]                           # close every open trade in AAPL, once you confirm
```

`status` needs `TRADINGAGENTS_ARENA_AGENT_ID`. `close` asks "Close these paper
trades on AlphArena (simulated money)? [y/N]" unless `--yes`, and records each
close in the ledger; AlphArena closes a trade in full, at its own price. Figures
are shown as AlphArena reports them.

The tables of `status`, `leaderboard` and `trades` are never cut to fit the
terminal, though AlphArena's text in them (an agent's name, say) is shortened to
at most 40 characters. With no terminal (a pipe, a log, Claude Code's Bash tool)
they are printed at their full width. At a terminal too narrow for a table, its
columns narrow only as far as their values allow (long names and headers wrap at
spaces); when that is not enough, each row is shown as label and value lines.

## Settings

| Variable | Key | Default | Meaning |
|---|---|---|---|
| `TRADINGAGENTS_ARENA` | `arena_enabled` | `False` | Offer decisions to AlphArena after a CLI run |
| `TRADINGAGENTS_ARENA_MODE` | `arena_mode` | `sandbox` | `sandbox` (`aa_test_` key) or `live` (`aa_live_` key); anything else stops the CLI at start-up |
| `TRADINGAGENTS_ARENA_AGENT_ID` | `arena_agent_id` | unset | Your agent id, for `arena status` |
| `ALPHARENA_API_KEY` | (never a config key) | unset | Read from the environment at each call |
| | `arena_buy_position_usd` | `1950` | Size of a Buy, and of a Sell's short |
| | `arena_overweight_position_usd` | `1000` | Size of an Overweight |
| | `arena_allow_short` | `False` | A Sell also opens a short |
| | `arena_send_thesis` | `False` | Send the executive summary (at most 300 characters) as the public thesis |
| | `arena_ledger_path` | unset | The ledger file; unset is `<results_dir>/arena/ledger.jsonl` |

None of these changes what a run decides, and none is part of a checkpoint's
identity or the report's settings.

## India is excluded

NSE and BSE decisions are refused before any request, with the reason:
"AlphArena trades US stocks only; Indian listings stay in the local paper log
(SEBI has cautioned against public paper-trading platforms built on Indian
listed-stock prices)". AlphArena does not take `.NS` or `.BO` symbols either,
and `tradingagents arena close` refuses them the same way. The sources, as read
on 2026-10-29:

- SEBI press release No. 27/2024 of 4 Nov 2024
  (https://www.sebi.gov.in/sebi_data/attachdocs/nov-2024/1730717924773.pdf):
  apps "offering virtual trading services or paper trading or fantasy games to
  the public based on stock price data of listed companies" are "in violation of
  Securities Contract (Regulation) Act, 1956 and SEBI Act, 1992".
- SEBI's circular of 24 May 2024 (SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/56) bars
  exchanges and intermediaries from sharing real-time price data with third
  parties, citing such platforms; its circular of 8 May 2026 raises the lag for
  price data used in education to 30 days from 1 Jul 2026.

Indian decisions stay in TradingAgents' own memory log, and intraday plans are
scored locally ([docs/intraday.md](intraday.md)). This is not legal advice.

## Limits

- Only the first 100 open trades are read when planning or closing.
- West of New York, from the hour New York's date moves on until the machine's
  own midnight (21:00 to midnight in California), no run can be sent: the CLI
  takes no date after the machine's, and AlphArena takes only New York's.
- A key with the right prefix that AlphArena refuses (revoked, say, or mistyped)
  is found only after the run, when the open trades are read: the CLI then shows
  AlphArena's error and sends nothing.
- If the ledger fails between requests, or Ctrl+C lands after AlphArena accepted
  a trade but before it was recorded, that trade is in neither the ledger nor the
  report; `tradingagents arena trades` lists what reached AlphArena.
- Whether AlphArena scopes the idempotency key per key or per mode is not
  documented; a close retried after a lost answer can come back `TRADE_NOT_OPEN`.
- During US market hours the "latest close" is Yahoo's partial daily bar, the
  latest price. Weekend and US-holiday runs dated New York's today are eligible,
  though how AlphArena fills outside market hours is not documented.
