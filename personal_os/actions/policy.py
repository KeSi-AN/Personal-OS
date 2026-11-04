"""Who may run what without asking (DESIGN.md §6.3, ADR-017).

| tier | you asked directly, untainted | proactive, or after captured content |
|---|---|---|
| 0 read-only | runs | runs |
| 1 low-risk | runs (if in auto_allow_tiers and not in confirm_tools) | asks |
| 2 | asks | asks |
| 3 | blocked; while unlocked, asks | blocked |

The kill switch blocks everything. Rate limits apply before asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from personal_os.actions.base import Origin, Tier, Tool
from personal_os.config.schema import ActionsConfig

Verdict = Literal["auto", "confirm", "block"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def decide(
    tool: Tool,
    origin: Origin,
    tainted: bool,
    cfg: ActionsConfig,
    *,
    killed: bool,
    unlocked: bool,
    recent: int,
    recent_tier2: int,
) -> Decision:
    if killed:
        return Decision(
            "block", "kill switch is on: observe-only until `personal-os actions resume`"
        )
    if recent >= cfg.rate_per_minute:
        return Decision("block", f"rate limit: {cfg.rate_per_minute} actions a minute")
    if tool.tier >= Tier.CONFIRM and recent_tier2 >= cfg.tier2_rate_per_minute:
        return Decision(
            "block", f"rate limit: {cfg.tier2_rate_per_minute} tier-2+ actions a minute"
        )
    direct = origin in (Origin.USER, Origin.PLANNED) and not tainted
    if tool.tier == Tier.BLOCKED:
        if not direct:
            return Decision(
                "block", "tier 3 is never run for proactive or content-derived requests"
            )
        if not unlocked:
            return Decision(
                "block", f"tier 3 is blocked (unlock with `personal-os actions unlock {tool.name}`)"
            )
        return Decision("confirm", "tier 3, unlocked: confirm every time")
    if tool.tier == Tier.CONFIRM:
        return Decision("confirm", "tier 2: confirm every time")
    if tool.tier == Tier.LOW:
        if not direct:
            why = "after captured content" if tainted else "not something you asked for"
            return Decision("confirm", f"tier 1, {why}")
        if tool.name in cfg.confirm_tools or Tier.LOW not in cfg.auto_allow_tiers:
            return Decision("confirm", "tier 1, set to always ask")
        return Decision("auto", "tier 1, asked for directly")
    return Decision("auto", "tier 0, read-only")
