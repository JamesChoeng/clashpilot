"""Switch decision state machine for autoswitch."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from . import config
from .bench import bench_nodes, is_benched
from .env_config import (
    MAX_DEFER,
    SWITCH_COOLDOWN,
    SWITCH_IMPROVEMENT_PCT,
    SWITCH_SUSTAIN_SECONDS,
)
from .health import is_alive
from .proxy_ctrl import has_active_target_connection

Action = Literal[
    "none",
    "kept",
    "pending",
    "cooldown",
    "deferred",
    "switched",
    "idle_skip",
]


@dataclass
class SwitchContext:
    group: str
    cur: str | None
    cur_score: float | None
    best: str
    best_score: float
    ranking: list[tuple[str, float]]
    nodes: list[str]
    last_switch_ts: float
    defer_count: int
    faster_candidate: str | None
    faster_since: float
    # When True, health already confirmed the current node is down — skip a
    # redundant is_alive re-check before failover.
    trusted_unhealthy: bool = False


@dataclass
class SwitchDecision:
    action: Action
    reason: str = ""
    force: bool = False
    to_node: str | None = None
    from_node: str | None = None
    unconfirmed: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


def significantly_faster(candidate_score: float, current_score: float) -> bool:
    if current_score <= 0:
        return False
    return (current_score - candidate_score) / current_score >= SWITCH_IMPROVEMENT_PCT / 100


def improvement_pct(candidate_score: float, current_score: float) -> int:
    if current_score <= 0:
        return 0
    return int((current_score - candidate_score) / current_score * 100)


def should_defer_switch(*, force: bool = False) -> bool:
    return not force and has_active_target_connection()


def decide(ctx: SwitchContext) -> SwitchDecision:
    cur = ctx.cur
    cur_score = ctx.cur_score
    best, best_score = ctx.best, ctx.best_score

    if cur is None:
        return SwitchDecision(
            action="switched",
            reason="no current node",
            force=True,
            to_node=best,
            from_node=None,
        )

    chain = config.pinned_chain()
    if chain:
        reachable_scores = {n: s for n, s in ctx.ranking}
        reachable_chain = [n for n in chain if n in reachable_scores]
        if reachable_chain:
            top_chain = reachable_chain[0]
            if cur == top_chain:
                return SwitchDecision(
                    action="kept",
                    reason="pinned",
                    payload={"node": cur, "best": best, "best_score": int(best_score)},
                )
            # A higher-priority chain member is reachable but we're not on it
            # (either failing over from a dead higher-priority node, or
            # restoring back up now that it recovered).
            return SwitchDecision(
                action="switched",
                reason="pinned failover" if cur_score is None else "pinned restore",
                force=(cur_score is None),
                to_node=top_chain,
                from_node=cur,
                payload={"to_score": int(reachable_scores[top_chain])},
            )
        # None of the pinned chain is reachable this round -- fall through to
        # normal autoswitch logic below (may bench/failover the current node).

    if cur_score is None:
        if config.opus_whitelist() is not None and cur not in ctx.nodes:
            if should_defer_switch():
                return SwitchDecision(
                    action="deferred",
                    reason="whitelist enforcement",
                    payload={"node": cur, "best": best, "best_score": int(best_score)},
                )
            return SwitchDecision(
                action="switched",
                reason="whitelist enforcement",
                force=True,
                to_node=best,
                from_node=cur,
            )
        if is_benched(cur):
            return SwitchDecision(
                action="switched",
                reason="benched",
                force=True,
                to_node=best,
                from_node=cur,
            )
        if not ctx.trusted_unhealthy and is_alive(cur):
            return SwitchDecision(
                action="kept",
                reason="alive but not ranked",
                payload={"node": cur, "best": best},
            )
        bench_nodes(cur, "confirmed dead at scan")
        return SwitchDecision(
            action="switched",
            reason="confirmed dead at scan",
            force=True,
            to_node=best,
            from_node=cur,
        )

    if best != cur and significantly_faster(best_score, cur_score):
        now = time.time()
        faster_candidate = ctx.faster_candidate
        faster_since = ctx.faster_since
        if best != faster_candidate:
            faster_since = now
        sustained = now - faster_since
        pct = improvement_pct(best_score, cur_score)
        if sustained < SWITCH_SUSTAIN_SECONDS:
            return SwitchDecision(
                action="pending",
                reason="sustain window",
                payload={
                    "node": cur,
                    "score": int(cur_score),
                    "best": best,
                    "best_score": int(best_score),
                    "improvement_pct": pct,
                    "sustained_s": int(sustained),
                    "required_s": SWITCH_SUSTAIN_SECONDS,
                    "_faster_candidate": best,
                    "_faster_since": faster_since,
                },
            )
        since = now - ctx.last_switch_ts
        if since < SWITCH_COOLDOWN:
            return SwitchDecision(
                action="cooldown",
                reason="switch cooldown",
                payload={
                    "node": cur,
                    "score": int(cur_score),
                    "best": best,
                    "best_score": int(best_score),
                },
            )
        if has_active_target_connection() and ctx.defer_count < MAX_DEFER:
            return SwitchDecision(
                action="deferred",
                reason="active connection",
                payload={
                    "node": cur,
                    "score": int(cur_score),
                    "best": best,
                    "best_score": int(best_score),
                    "defer_count": ctx.defer_count + 1,
                },
            )
        forced = ctx.defer_count >= MAX_DEFER
        return SwitchDecision(
            action="switched",
            reason="optimization",
            force=forced,
            to_node=best,
            from_node=cur,
            payload={
                "from_score": int(cur_score),
                "to_score": int(best_score),
                "improvement_pct": pct,
                "sustained_s": int(sustained),
                "forced": forced,
                "_reset_faster": True,
                "_reset_defer": forced,
            },
        )

    return SwitchDecision(
        action="kept",
        reason="not significantly faster",
        payload={
            "node": cur,
            "score": int(cur_score),
            "best": best,
            "best_score": int(best_score),
            "_reset_faster": True,
            "_reset_defer": True,
        },
    )
