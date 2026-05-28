from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Token:
    mint: str
    symbol: str
    name: str
    launchpad: str                          # "pump.fun" | "bags" | "unknown"
    created_at: int                         # unix epoch
    market_cap_usd_snapshot: Optional[float] = None
    holders_count_snapshot: Optional[int] = None
    lp_burned: bool = False
    top10_pct: Optional[float] = None       # % supply held by top 10
    bundle_pct: Optional[float] = None      # % bought in coordinated bundle
    dev_pct: Optional[float] = None         # % held by detected dev cluster
    narrative_tags: list[str] = field(default_factory=list)


@dataclass
class Wallet:
    address: str
    label: Optional[str] = None
    smart_money_score: float = 0.0          # 0–1
    win_rate_90d: Optional[float] = None
    total_trades: int = 0
    first_seen: Optional[int] = None        # unix epoch
    funding_source: Optional[str] = None    # funder address or "cex"


@dataclass
class WalletCluster:
    cluster_id: str                         # UUID
    funding_source: str
    funded_at: Optional[int] = None         # unix epoch — window start
    member_addresses: list[str] = field(default_factory=list)
    is_likely_team: bool = False
    funded_window_end: Optional[int] = None # unix epoch — window end
    total_sol_funded: float = 0.0           # SOL received by all members from funder


@dataclass
class TokenBuyer:
    token_mint: str
    wallet_address: str
    bought_at: int                          # unix epoch
    sol_amount: float
    tokens_received: float
    position_size_pct: Optional[float] = None
    exit_price_sol: Optional[float] = None
    exit_at: Optional[int] = None


@dataclass
class Trade:
    """Represents one of my own trades, as ingested from chain or manually tagged."""

    tx_signature: str
    token_mint: str
    side: str                               # "buy" | "sell"
    ts: int                                 # unix epoch
    sol_amount: float
    tokens: float
    price_sol: float
    mc_at_entry: Optional[float] = None
    holders_at_entry: Optional[int] = None
    smart_money_in_count_at_entry: Optional[int] = None
    lp_burned: Optional[bool] = None
    top10_pct: Optional[float] = None
    bundle_pct: Optional[float] = None
    dev_pct: Optional[float] = None
    source_tag: Optional[str] = None        # e.g. "smart_money_alert", "manual"
    conviction: Optional[int] = None        # 1–5
    rules_followed: list[str] = field(default_factory=list)
    exit_reason: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class NarrativeState:
    id: int
    label: str
    keywords: list[str]
    started_at: int                         # unix epoch
    peak_velocity: float = 0.0             # mentions/hour at peak
    current_velocity: float = 0.0          # mentions/hour rolling 1h
    status: str = "emerging"               # "emerging" | "hot" | "fading" | "dead"


@dataclass
class NarrativeMention:
    narrative_id: int
    x_handle: str
    posted_at: int                          # unix epoch
    follower_count: Optional[int] = None
    text_excerpt: Optional[str] = None
