# Agent-Reach web research (optional)

[Agent-Reach](https://github.com/Panniantong/Agent-Reach) is a separate
command-line tool that puts Exa web search, Jina Reader and YouTube (through
yt-dlp) behind one command. With it switched on, TradingAgents reads company and
global news from Exa's web search first, falling back to Yahoo, and the News
Analyst can also read a web page, list a topic's YouTube videos and read a
video's captions. It needs no account or API key. It is off by default, and with
it off nothing in a run changes.

TradingAgents uses only Agent-Reach's no-login channels, through five read-only
actions. Agent-Reach can do much more, and some of it is unsafe to run on your
machine (see [Never run](#never-run)). Install it the isolated way below, not the
way its own guide describes. This page was checked on 2026-10-29 against
Agent-Reach v1.6.0; it runs on macOS and Linux, not Windows.

## What TradingAgents runs

| TradingAgents tool | Agent-Reach call | Service it reaches |
|---|---|---|
| `get_news`, `get_global_news` (news_data) | `web search --limit N -- <query>` | Exa (`mcp.exa.ai`), without a key |
| `read_web_page` | `web read --backend jina -- <url>` | Jina Reader (`r.jina.ai`), never the Exa fallback |
| `get_video_coverage` | `youtube search --limit N -- <query>`, then `youtube read` per video | YouTube, through yt-dlp |
| `get_video_transcript` | `youtube read`, then `youtube subtitle --lang en,en-orig,hi` | YouTube, through yt-dlp |

Every call carries `--timeout` (`agent_reach_timeout`). Each process also runs
`agent-reach version` once, and refuses a program that does not answer as
Agent-Reach v1.6 or later. Any other action is refused before anything starts.
Agent-Reach runs without a shell, in its own HOME, with exactly six environment
variables: `HOME`, `PATH` (`/usr/bin:/bin`, the folder holding `node` if you
have it, and Agent-Reach's own folder), `PYTHONSAFEPATH=1`, `PYTHONUTF8=1`,
`LANG` and `LC_ALL`. None of your API keys, tokens or proxy settings reach it,
or the yt-dlp and node it starts.

Everything it returns is third-party text. Each output is labelled as untrusted
and cleaned of control, bidi and invisible tag characters. Titles and names are
kept to one short line; snippets, page text and captions are cut to lengths you
set and quoted line by line. A web page's images are left out and its links
shown as their text before it is cut, so a news site's menus and ad links do not
use up the length. Agent-Reach's error messages, hints and update
notices are written for an AI agent to act on; TradingAgents logs them at DEBUG
only and never passes them to a model.

## Install (isolated and pinned)

Install the reviewed release file, not a package index. The release asset
`agent-reach.zip` of v1.6.0 is a plain source archive of tag v1.6.0 (commit
`beabda7486e9a666360b18cdb5c0e759c2c4d20f`, which `unzip -z` prints), 676,536
bytes, with this SHA-256:

```
59c37586e45e5e111a78686e98a9d11ae71a7b4b1a334c41aef82831ea5b0a05
```

> **Never run `pip install agent-reach` or `uv tool install agent-reach`.** The
> package named `agent-reach` on PyPI is a different project. Agent-Reach's own
> install guide warns about it too, and TradingAgents' version check refuses it.
>
> **Do not follow Agent-Reach's own install guide or run `agent-reach setup`.**
> The guide is written for an AI to "run every command yourself", pipes a
> download into `sh`, and installs the unpinned latest release; `setup` and
> `update` link Agent-Reach's AI skill into `~/.claude/skills` and up to six
> other agents' skill folders. Do not install that skill into Claude Code.

1. Download the release file and check it. It is unpacked only when the check
   prints `agent-reach.zip: OK`; if it prints `FAILED`, stop and delete the file.

   ```sh
   AR_DIR="$HOME/.tradingagents/agent-reach"         # source, tools and cache
   AR_HOME="$HOME/.tradingagents/agent-reach-home"   # Agent-Reach's own HOME (TradingAgents' default)
   mkdir -p -m 700 "$AR_DIR" "$AR_HOME"
   cd "$AR_DIR"
   curl -fL -o agent-reach.zip https://github.com/Panniantong/Agent-Reach/releases/download/v1.6.0/agent-reach.zip
   echo "59c37586e45e5e111a78686e98a9d11ae71a7b4b1a334c41aef82831ea5b0a05  agent-reach.zip" |
     shasum -a 256 -c - && unzip -q agent-reach.zip -d src   # on Linux: sha256sum -c -; creates src/agent-reach
   SRC="$AR_DIR/src/agent-reach"
   ```

2. Install it with [uv](https://docs.astral.sh/uv/) (reviewed with uv 0.12), from
   that folder, in an empty environment:

   ```sh
   UV="$(command -v uv)"
   PY=/full/path/to/python3.13   # any Python 3.10 or later; `uv python find` prints one uv knows
   env -i HOME="$AR_HOME" PATH="/usr/bin:/bin" \
     UV_TOOL_DIR="$AR_DIR/env/tools" UV_TOOL_BIN_DIR="$AR_DIR/env/bin" \
     UV_CACHE_DIR="$AR_DIR/env/cache" UV_PYTHON_INSTALL_DIR="$AR_DIR/env/python" \
     "$UV" tool install --no-config --no-python-downloads --python "$PY" \
       --constraints "$SRC/constraints.txt" --exclude-newer 2026-10-24T00:00:00Z "$SRC"
   ```

   - `env -i` keeps your keys and tokens out of the install, and the `UV_*`
     folders keep everything under `$AR_DIR`.
   - `--no-config` ignores your uv settings; `--no-python-downloads` with
     `--python` uses a Python you already have.
   - `--constraints` applies the five pins Agent-Reach ships (requests, loguru,
     PyYAML, yt-dlp 2026.8.19, yt-dlp-ejs 0.8.0). `--exclude-newer` resolves the
     unpinned dependencies as they stood the day after the release.
   - The program is `$AR_DIR/env/bin/agent-reach`. That folder is not on your
     PATH; leave it so (no `uv tool update-shell`), since TradingAgents is given
     the full path.
   - Use a folder whose path has no symlink in it, such as the one above: on
     macOS `/tmp` is a symlink, and Agent-Reach then fails after each call.
     TradingAgents resolves `agent_reach_home` through symlinks itself.

3. Try it by hand with the same kind of environment TradingAgents gives it:

   ```sh
   AR() { env -i HOME="$AR_HOME" PATH="/opt/homebrew/bin:/usr/bin:/bin" PYTHONSAFEPATH=1 \
          "$AR_DIR/env/bin/agent-reach" "$@"; }
   AR version          # one JSON line with "version": "1.6.0"
   AR channels
   AR web search "Reliance Industries quarterly results" --limit 5
   AR web read https://example.com --backend jina
   AR youtube search "Nifty 50 outlook" --limit 5
   AR youtube read jNQXAC9IVRw
   AR youtube subtitle https://www.youtube.com/watch?v=jNQXAC9IVRw --lang en,en-orig
   ```

   `/opt/homebrew/bin` is there only so yt-dlp can run YouTube's JavaScript
   challenges in `node` (that is Homebrew's folder on Apple silicon; use the
   folder that holds your `node`, or leave it out to never run YouTube's
   JavaScript, which may make YouTube results worse).

To remove everything: `rm -rf ~/.tradingagents/agent-reach ~/.tradingagents/agent-reach-home`.

### Never run

TradingAgents never calls these, and you should not either, on this install or
any other:

- `setup`, or `update` in any form: they link Agent-Reach's skill into
  `~/.claude/skills` and other agents' folders, and `update` reinstalls itself
  from an unpinned GitHub tag (and can fetch and run uv's install script).
- `add`, `remove`, `login`, `logout`, `configure`, `browser`, `skill link`.
- A bare `check` (use `check web youtube`).
- The generic `read <url>` for anything but YouTube (use `web read`): when the
  public page fails it sends reddit, xiaohongshu, LinkedIn job and zhipin links
  into the browser chain, and it reads github.com links through your own `gh`
  login.
- `youtube transcribe`: it uploads audio to Groq or OpenAI with your key.
- Any login or browser channel, `bilibili subtitle` and `github`: on first use the
  browser channels install CloakBrowser and an unpinned OpenCLI browser extension,
  and the login channels can copy site cookies from a connected Chrome.
- If you run `doctor`, ignore its "ffmpeg not installed" hint and its
  suggestion to run `agent-reach add youtube`: ffmpeg is needed only for
  `transcribe`.

## Turn it on

In `.env` in the repository folder, or exported in the shell:

```bash
TRADINGAGENTS_AGENT_REACH=1
TRADINGAGENTS_AGENT_REACH_COMMAND=~/.tradingagents/agent-reach/env/bin/agent-reach
# TRADINGAGENTS_AGENT_REACH_HOME=~/.tradingagents/agent-reach-home   (the default)
```

Then run as usual, for example
`tradingagents --ticker RELIANCE.NS --analysts market,news --save --no-show`.
With it on:

- **News.** When `news_data` is still the default `yfinance`, the run reads
  `agent_reach,yfinance`: Exa first, and Yahoo when Agent-Reach is missing, a
  search fails or nothing it found is dated in the window. The Sentiment Analyst,
  given the same company news, heads Exa's results as unverified web pages, not
  as a news publisher's headlines. A `news_data` you set in a config, or with
  `--vendor news_data=...`, is kept as you set it, except `yfinance` itself in a
  Python config that switches Agent-Reach on (below). The report's "Data
  vendors" line names the chain the run used, and the run settings in the state
  log record `agent_reach_enabled`.
- **Web pages and videos.** The News Analyst also gets `read_web_page`,
  `get_video_coverage` and `get_video_transcript`, with a sentence on what they
  are for and one saying that their output is third-party content.

Switched off, the News Analyst's tools and prompt are as they were, and
Agent-Reach is never started. Naming it in a chain uses it even when switched off:
`--vendor news_data=agent_reach,yfinance` reads news through it for one run,
without the web tools. Agent-Reach serves no insider transactions, which share
the `news_data` category, so `--vendor news_data=agent_reach` alone stops the
run before it starts. From Python, set `config["agent_reach_enabled"] = True`
(and `agent_reach_command`). There a `news_data` of `yfinance` cannot be told from
the default, so it reads `agent_reach,yfinance` too, unless the environment's
switch is also on; to keep Yahoo's news while the News Analyst gets the web tools,
set `config["tool_vendors"] = {"get_news": "yfinance", "get_global_news": "yfinance"}`.
Setting it to `False` switches off a
`TRADINGAGENTS_AGENT_REACH=1` the environment set: a `news_data` still at the
`agent_reach,yfinance` default that switch gave it reads `yfinance` again, while
any other chain the config names is kept.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `agent_reach_enabled` | `False` | `TRADINGAGENTS_AGENT_REACH` (`1`, `true`, `yes` or `on`) |
| `agent_reach_command` | `"agent-reach"` | The program: a name found on your PATH, or a path (a leading `~` is expanded). Not a command line: no arguments, no shell. `TRADINGAGENTS_AGENT_REACH_COMMAND` |
| `agent_reach_home` | `"~/.tradingagents/agent-reach-home"` | Agent-Reach's own HOME, created owner-only (0700) when missing. `TRADINGAGENTS_AGENT_REACH_HOME` |
| `agent_reach_timeout` | `30` | Seconds per call (TradingAgents stops waiting 30 s later). Raise it if YouTube calls time out. |
| `agent_reach_max_results` | `5` | Results per search; news searches ask for twice this |
| `agent_reach_snippet_chars` | `600` | Characters kept of each news snippet |
| `agent_reach_page_chars` | `6000` | Characters kept of a web page |
| `agent_reach_transcript_chars` | `8000` | Characters kept of a video's captions |
| `agent_reach_video_lookback_days` | `30` | How far back `get_video_coverage` lists videos |
| `agent_reach_india_queries` | 8 searches | The India section's searches for an NSE/BSE listing: RBI policy, Nifty and Sensex, FII/DII flows, the rupee, inflation, SEBI, the Union Budget, crude |

Company news searches for `"<company name> stock news"` (with `India` added for
an NSE/BSE listing, and `crypto news` for a coin), taking the name from Yahoo's
profile, or the ticker when Yahoo has none. Global news searches
`global_news_queries`; for an NSE/BSE listing it reports an India section from
`agent_reach_india_queries` (whatever Yahoo's `global_news_queries_india` holds)
and a Global section, sharing `global_news_article_limit` as the Yahoo vendor
does. Exa's semantic search suits these multi-word topics, unlike Yahoo's
keyword search ([docs/india.md](india.md)).

## Dates and look-ahead

Exa's search has no date filter and answers from today's index, and the dates it
gives come from the pages themselves. A run is dated in the past when its date
is before today: your machine's date, or the listing's when that is earlier. On
a machine in India after midnight, a US run dated New York's day is a run dated
today.

The window's days, and the dates shown, are the listing's: India's for an
NSE/BSE listing, New York's for a US listing, UTC's otherwise. A time with a zone
falls on its day there, so a past NSE run reads nothing timed after its date has
ended in India. A date with no time, or exactly midnight (how Exa writes a
page that states only a date, such as an RBI press release), counts as that date
in every listing, and so does a video's upload date. A time with no zone counts
on the later of the date it states and its date read as UTC in the listing's
zone, so a past run never reads it early, but is shown on the date it states,
as `read_web_page` shows it: for an NSE listing, a story timed
"2026-10-29T21:00" counts as 30 Oct (21:00 UTC is 02:30 IST the next day) and
is listed as published 2026-10-29.

- **A run dated in the past** keeps only items dated inside its window, and drops
  undated ones. A date with no day or year ("March 2026"), a relative word
  ("today"), an ambiguous numeric date (05/03/2026, also after a weekday or a
  time: "Sun 03/05/2026") or a year before 1900 or after 2200 (0001-01-01, which
  Hugo writes for an undated page, or 9999-12-31) counts as undated. An offset
  after GMT or UTC ("GMT-4", "UTC+5:30") is read as hours from UTC, as pages
  mean it.
- **A run dated today** also keeps undated items, marked "(date not given)".
  Nothing dated after your machine's date is kept.
- **Web pages:** a live page shows today's content, so `read_web_page` refuses a
  run dated in the past. Its "Published:" line gives a time with a zone in the
  listing's zone, named ("2026-10-30 06:00 IST"), a date alone or midnight as
  "2026-10-30 (as the page states)", and a time with no zone as "2026-10-30 06:00
  (as the page states, with no time zone)"; an undated page has no such line.
- **Videos** are dated by their upload (search results carry no date), with the
  same bounds as news: a past-dated run lists and transcribes only videos
  uploaded by its date, never one without an upload date, and a run dated today
  none uploaded after your machine's date. A past-dated run refused a later
  video's captions is not told when it was uploaded. When a `youtube read` fails
  part-way through a list (a rate limit, YouTube's bot check), the videos already
  read are listed and the rest counted as not checked.

A wrong date on a page cannot be detected: on 2026-10-29 a search returned a
page titled "Q2 FY27 results" with a date of 2026-07-17. Web search is most
reliable on runs dated today. When nothing it found is dated inside the window,
the next vendor in the news chain answers instead (Yahoo, in the default
`agent_reach,yfinance`). If no vendor in the chain has anything, the tool says
the news for the window is unavailable, not that there was none: a search with no
date filter that finds nothing has not shown an absence of news. A backtest never
uses Agent-Reach: it switches it off, takes it out of every vendor chain (a news
chain it alone made up reads `yfinance`) and says so once in its log, since every
cell is past-dated and a sweep would hit Exa's limit for anonymous use.

## What Exa, Jina and YouTube see

- **Exa:** each search query: `"<company name> stock news"`, your
  `global_news_queries` and, for NSE/BSE listings, `agent_reach_india_queries`.
- **Jina Reader:** the address of each page `read_web_page` reads, usually a link
  a news tool returned.
- **YouTube:** the searches the News Analyst writes for `get_video_coverage`, and
  the videos it reads.
- **All of them:** your IP address, as with any web request. Their data-retention
  and logging policies were not reviewed.

Nothing else is sent: no key, no portfolio, no report text. The News Analyst
chooses the page links and video searches and is not given your portfolio. Do
not configure queries that describe your holdings.

On these calls Agent-Reach writes only under its HOME: `.agent-reach/state.json`
(mode 0600: error codes, times and a fingerprint, never a query or a result),
lock files and a temporary folder for captions; yt-dlp may keep its cache there
too.

## Limits

- **Rate limits.** A rate limit (`RATE_LIMITED`) pauses Agent-Reach's whole
  channel for about an hour: an Exa limit also blocks `web read`. It clears only
  when it expires; do not run `agent-reach remove` to clear it.
- **Empty searches.** Exa reports a search with no usable results as a failure
  (`UPSTREAM_BROKEN`), and Agent-Reach then pauses Exa for 120 s, so one empty
  query can send that call's news to Yahoo.
- **Searches per call.** An India run's global news can make up to 13 Exa
  searches (8 India and 5 Global) when few results fall in the window, which is
  typical for past dates. Exa's endpoint is anonymous, so that risks its limit.
- **Captions.** YouTube may refuse caption downloads without a login: on
  2026-10-29 a `youtube subtitle` call came back `RATE_LIMITED` (HTTP 429), which
  paused the youtube channel for an hour. The transcript tool then reports the
  captions as unavailable.
- **Versions.** Only v1.6.0 was reviewed. Later versions are accepted; one whose
  output no longer has the v1 shape is refused as "unexpected output".
- **Addresses.** `read_web_page` refuses non-http(s) addresses, user names in
  URLs, local and private hosts and IP addresses, but does not look names up, so
  a public name pointing at a private address passes. The page is fetched by
  Jina's servers, not from this machine.
- **Company name.** Finding the name costs one Yahoo profile request per ticker,
  kept for the process.

## Troubleshooting

A setup problem (the first three rows, or a bad setting) is logged as a warning
that names the tool and gives the message, such as "Vendor 'agent_reach' not
configured for get_news: Agent-Reach is not installed, ...; trying next
vendor.", and a failed call likewise as "Vendor 'agent_reach' unavailable for
get_news: ...; trying next vendor.". News then comes from the next vendor in the
chain; a web tool, which has no other vendor, tells the News Analyst the data is
unavailable and why.

| Message | What to do |
|---|---|
| `Agent-Reach is not installed, or agent_reach_command does not point to it` | Set `TRADINGAGENTS_AGENT_REACH_COMMAND` to the program's path, `~/.tradingagents/agent-reach/env/bin/agent-reach` after the install above. |
| `The agent-reach command is not Agent-Reach v1.6+; the PyPI package named agent-reach is a different project` | Remove that package and install as above. The check is made once per process, so restart after fixing it. |
| `Agent-Reach's home directory (agent_reach_home) could not be created` | Point `TRADINGAGENTS_AGENT_REACH_HOME` at a folder you can write. |
| `... rate-limited the request; Agent-Reach pauses its web (or youtube) channel for about an hour, so try later (RATE_LIMITED)` | Wait. News falls back to Yahoo meanwhile. |
| `... failed: the service behind it failed (UPSTREAM_BROKEN)` | Often an empty search, or Exa paused for 120 s after one; news falls back to Yahoo. |
| `... did not finish within N s` or `(TIMEOUT)` | Raise `agent_reach_timeout`. |
| `Agent-Reach returned unexpected output` | A version whose output TradingAgents does not know; use v1.6.0. |
| `DATA_UNAVAILABLE: optional web_research could not be retrieved (agent_reach is switched off; set agent_reach_enabled to use it)` | Set `TRADINGAGENTS_AGENT_REACH=1`. |
| `read_web_page is not available for a run dated ...` | Expected on a past-dated run: a live page postdates it. |
| `NO_DATA_AVAILABLE: No configured vendor found news for ... in the requested window (no Agent-Reach web search (Exa) result ...)` | Nothing in the news chain was dated in the window, which is no sign that there was no news. Keep `yfinance` in the chain, e.g. `--vendor news_data=agent_reach,yfinance`. |
