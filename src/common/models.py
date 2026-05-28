from dataclasses import dataclass, field
from enum import Enum
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


# ── Graduation-context models ─────────────────────────────────────────────────

class DistributionSignal(str, Enum):
    ACCUMULATING = "ACCUMULATING"   # wallets still buying post-graduation
    HOLDING      = "HOLDING"        # minimal movement — staying positioned
    DISTRIBUTING = "DISTRIBUTING"   # selling is accelerating — use caution
    DUMPED       = "DUMPED"         # token effectively dead, liquidity gone


@dataclass
class GraduationEvent:
    token_mint: str
    graduated_at: int               # unix epoch
    graduation_mc_usd: Optional[float] = None  # ~$69K at graduation
    sol_raised: Optional[float] = None          # ~85 SOL raised on BC
    detection_lag_seconds: int = 0
    pumpswap_pool_address: Optional[str] = None
    bc_top_holders: list[dict] = field(default_factory=list)  # [{wallet, pct}]


@dataclass
class WalletStats:
    address: str
    graduated_calls: int = 0        # BC purchases on coins that later graduated
    wins: int = 0                   # moon outcomes at 4h
    losses: int = 0                 # rug outcomes at 4h
    total_calls: int = 0
    win_rate: Optional[float] = None  # None when total_calls < 15
    last_updated: int = 0


@dataclass
class FunderReputation:
    funding_source: str
    graduated_mints: list[str] = field(default_factory=list)
    rug_count: int = 0
    moon_count: int = 0
    ok_count: int = 0
    rug_rate: float = 0.0
    moon_rate: float = 0.0
    avg_bundle_pct: float = 0.0
    avg_dev_pct: float = 0.0
    last_seen: Optional[int] = None
    is_known_rugger: bool = False   # True when rug_rate > 0.65 and len >= 8


@dataclass
class TeamCluster:
    """Post-graduation team cluster — richer than WalletCluster."""
    cluster_id: str
    token_mint: str
    funding_source: Optional[str] = None
    member_addresses: list[str] = field(default_factory=list)
    supply_pct_at_graduation: float = 0.0   # % of supply held at graduation
    first_buy_offset_seconds: float = 0.0   # seconds after launch first member bought
    is_bc_sniper: bool = False              # first buy within 30s of launch


@dataclass
class PostGradBehavior:
    token_mint: str
    checked_at: int
    check_offset_h: int             # 1, 4, or 24
    holders_remaining_count: Optional[int] = None
    team_sold_pct: Optional[float] = None    # % of team cluster position sold
    snipers_sold_pct: Optional[float] = None
    liquidity_usd: Optional[float] = None
    distribution_signal: DistributionSignal = DistributionSignal.HOLDING


@dataclass
class StructuralRead:
    """Verdict produced by the graduation analysis pipeline."""
    verdict: str                    # "SKIP" | "WATCH" | "STRUCTURALLY_SOUND"
    confidence: float               # 0.0–1.0
    dominant_factors: list[str]    # top reasons driving the verdict
    what_would_change: str          # signal that would flip the verdict
    bundle_pct: float = 0.0
    dev_pct: float = 0.0
    distribution_signal: Optional[str] = None
    funder_is_known_rugger: bool = False
    smart_money_count: int = 0
