"""Agent-Reach: web search, web pages and YouTube through the Agent-Reach CLI, no-login channels only.

``client`` runs the CLI (an allowlist of five read-only actions, an isolated HOME
and environment); ``news`` serves get_news and get_global_news; ``media`` serves
read_web_page, get_video_coverage and get_video_transcript.
"""

from tradingagents.dataflows.vendors.agent_reach.media import (
    get_video_coverage,
    get_video_transcript,
    read_web_page,
)
from tradingagents.dataflows.vendors.agent_reach.news import (
    get_global_news_agent_reach,
    get_news_agent_reach,
)

__all__ = [
    "get_global_news_agent_reach",
    "get_news_agent_reach",
    "get_video_coverage",
    "get_video_transcript",
    "read_web_page",
]
