"""Rate, concurrency and budget enforcement.

Counters are derived from durable rows (invocations, tasks) rather than process
memory, so limits survive a restart and stay correct across worker processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core import db
from app.core.errors import ApiError
from app.deps import AgentPrincipal


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    code: str = ""
    message: str = ""
    actions: tuple[str, ...] = ()
    details: dict[str, Any] | None = None
    retry_after_seconds: int = 0


def _window_seconds(window: str) -> int:
    return {"minute": 60, "day": 86400, "month": 2592000, "hour": 3600}.get(window, 86400)


def _since(window: str) -> str:
    from datetime import timedelta

    return db.iso(db.utcnow() - timedelta(seconds=_window_seconds(window)))


def _policies(scope_type: str, scope_id: str | None) -> list[dict[str, Any]]:
    if scope_id:
        return db.query_all(
            "SELECT * FROM budget_policies WHERE enabled = 1 AND scope_type = ? AND (scope_id = ? OR scope_id IS NULL)",
            (scope_type, scope_id),
        )
    return db.query_all(
        "SELECT * FROM budget_policies WHERE enabled = 1 AND scope_type = ? AND scope_id IS NULL", (scope_type,)
    )


def _agent_usage(agent_id: str, window: str) -> dict[str, float]:
    since = _since(window)
    row = db.query_one(
        """SELECT COUNT(*) AS calls,
                  COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd, 0)), 0) AS cost
             FROM invocations
            WHERE agent_id = ? AND created_at >= ? AND status != 'rejected'""",
        (agent_id, since),
    ) or {}
    return {"calls": float(row.get("calls") or 0), "cost": float(row.get("cost") or 0)}


def _agent_concurrency(agent_id: str) -> int:
    running = db.query_scalar(
        "SELECT COUNT(*) FROM invocations WHERE agent_id = ? AND status = 'running'", (agent_id,)
    ) or 0
    tasks = db.query_scalar(
        "SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status IN ('queued','running','cancel_requested')", (agent_id,)
    ) or 0
    return int(running) + int(tasks)


def check_agent_limits(principal: AgentPrincipal) -> LimitDecision:
    if not principal.token_id:
        return LimitDecision(True)

    token = db.query_one(
        "SELECT rate_limit_per_min, max_concurrency, budget_usd FROM agent_tokens WHERE id = ?",
        (principal.token_id,),
    ) or {}

    per_minute = token.get("rate_limit_per_min")
    if per_minute:
        used = db.query_scalar(
            "SELECT COUNT(*) FROM invocations WHERE agent_id = ? AND created_at >= ?",
            (principal.agent_id, _since("minute")),
        ) or 0
        if used >= per_minute:
            return LimitDecision(
                False,
                code="rate_limited",
                message=f"This token is limited to {per_minute} calls per minute.",
                actions=("Retry after a short wait.", "Ask the administrator to raise the limit."),
                details={"limit": per_minute, "used": int(used), "window": "1m"},
                retry_after_seconds=60,
            )

    max_concurrency = token.get("max_concurrency")
    if max_concurrency:
        active = _agent_concurrency(principal.agent_id)
        if active >= max_concurrency:
            return LimitDecision(
                False,
                code="concurrency_limited",
                message=f"This token allows {max_concurrency} concurrent call(s).",
                actions=("Wait for an in-flight call to finish.",),
                details={"limit": max_concurrency, "active": active},
                retry_after_seconds=5,
            )

    for window in ("minute", "day", "month"):
        usage = _agent_usage(principal.agent_id, window)
        for policy in _policies("agent", principal.agent_id):
            if policy["window"] != window:
                continue
            if policy.get("max_calls") and usage["calls"] >= policy["max_calls"]:
                return LimitDecision(
                    False,
                    code="budget_exceeded",
                    message=f"Call budget for this agent is exhausted ({int(usage['calls'])}/{policy['max_calls']} per {window}).",
                    actions=("Raise the budget in Access → Tokens.", "Or wait for the next window."),
                    details={"used": usage["calls"], "limit": policy["max_calls"], "window": window},
                )
            if policy.get("max_cost_usd") and usage["cost"] >= policy["max_cost_usd"]:
                return LimitDecision(
                    False,
                    code="budget_exceeded",
                    message=f"Spend budget for this agent is exhausted (${usage['cost']:.2f}/${policy['max_cost_usd']:.2f} per {window}).",
                    actions=("Raise the budget in Access → Tokens.",),
                    details={"used": usage["cost"], "limit": policy["max_cost_usd"], "window": window},
                )

    token_budget = token.get("budget_usd")
    if token_budget:
        usage = _agent_usage(principal.agent_id, "month")
        if usage["cost"] >= float(token_budget):
            return LimitDecision(
                False,
                code="budget_exceeded",
                message=f"This token's monthly spend cap is reached (${usage['cost']:.2f}/${float(token_budget):.2f}).",
                actions=("Ask the administrator to raise the cap.",),
                details={"used": usage["cost"], "limit": float(token_budget)},
            )
    return LimitDecision(True)


def check_capability_limits(capability_id: str, provider_id: str | None) -> LimitDecision:
    for scope_type, scope_id in (("capability", capability_id), ("provider", provider_id)):
        if not scope_id:
            continue
        for policy in _policies(scope_type, scope_id):
            since = _since(policy["window"])
            if scope_type == "capability":
                row = db.query_one(
                    "SELECT COUNT(*) AS calls, COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd,0)),0) AS cost "
                    "FROM invocations WHERE capability_id = ? AND created_at >= ?",
                    (scope_id, since),
                ) or {}
            else:
                row = db.query_one(
                    "SELECT COUNT(*) AS calls, COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd,0)),0) AS cost "
                    "FROM invocations WHERE provider_id = ? AND created_at >= ?",
                    (scope_id, since),
                ) or {}
            calls = float(row.get("calls") or 0)
            cost = float(row.get("cost") or 0)
            if policy.get("max_calls") and calls >= policy["max_calls"]:
                return LimitDecision(
                    False,
                    code="budget_exceeded",
                    message=f"{scope_type.title()} call budget exhausted ({int(calls)}/{policy['max_calls']} per {policy['window']}).",
                    actions=("Adjust the policy in Settings → Limits.",),
                    details={"scope": scope_type, "used": calls, "limit": policy["max_calls"]},
                )
            if policy.get("max_cost_usd") and cost >= policy["max_cost_usd"]:
                return LimitDecision(
                    False,
                    code="budget_exceeded",
                    message=f"{scope_type.title()} spend budget exhausted (${cost:.2f}/${policy['max_cost_usd']:.2f}).",
                    actions=("Adjust the policy in Settings → Limits.",),
                    details={"scope": scope_type, "used": cost, "limit": policy["max_cost_usd"]},
                )
    return LimitDecision(True)


def usage_overview(days: int = 7) -> dict[str, Any]:
    from datetime import timedelta

    since = db.iso(db.utcnow() - timedelta(days=days))
    totals = db.query_one(
        """SELECT COUNT(*) AS calls,
                  SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                  AVG(CASE WHEN latency_ms IS NOT NULL THEN latency_ms END) AS avg_latency,
                  COALESCE(SUM(COALESCE(cost_actual_usd, cost_estimate_usd, 0)), 0) AS cost
             FROM invocations WHERE created_at >= ?""",
        (since,),
    ) or {}
    by_capability = db.query_all(
        """SELECT c.slug, c.name, COUNT(*) AS calls,
                  SUM(CASE WHEN i.status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                  AVG(i.latency_ms) AS avg_latency,
                  COALESCE(SUM(COALESCE(i.cost_actual_usd, i.cost_estimate_usd,0)),0) AS cost
             FROM invocations i LEFT JOIN capabilities c ON c.id = i.capability_id
            WHERE i.created_at >= ? GROUP BY c.slug, c.name ORDER BY calls DESC LIMIT 20""",
        (since,),
    )
    by_provider = db.query_all(
        """SELECT p.name AS provider, COUNT(*) AS calls,
                  COALESCE(SUM(COALESCE(i.cost_actual_usd, i.cost_estimate_usd,0)),0) AS cost
             FROM invocations i LEFT JOIN providers p ON p.id = i.provider_id
            WHERE i.created_at >= ? GROUP BY p.name ORDER BY calls DESC""",
        (since,),
    )
    by_agent = db.query_all(
        """SELECT a.name AS agent, COUNT(*) AS calls,
                  COALESCE(SUM(COALESCE(i.cost_actual_usd, i.cost_estimate_usd,0)),0) AS cost
             FROM invocations i JOIN agents a ON a.id = i.agent_id
            WHERE i.created_at >= ? GROUP BY a.name ORDER BY calls DESC""",
        (since,),
    )
    calls = int(totals.get("calls") or 0)
    succeeded = int(totals.get("succeeded") or 0)
    return {
        "window_days": days,
        "calls": calls,
        "succeeded": succeeded,
        "failed": int(totals.get("failed") or 0),
        "success_rate": round(succeeded / calls, 4) if calls else None,
        "avg_latency_ms": round(float(totals.get("avg_latency") or 0), 1) if calls else None,
        "estimated_cost_usd": round(float(totals.get("cost") or 0), 6),
        "by_capability": by_capability,
        "by_provider": by_provider,
        "by_agent": by_agent,
    }


def current_counters(agent_id: str | None = None) -> dict[str, Any]:
    now_window = _since("minute")
    if agent_id:
        calls = db.query_scalar(
            "SELECT COUNT(*) FROM invocations WHERE agent_id = ? AND created_at >= ?", (agent_id, now_window)
        ) or 0
        return {"agent_id": agent_id, "calls_last_minute": int(calls), "concurrency": _agent_concurrency(agent_id)}
    decisions = db.query_one(
        "SELECT COUNT(*) AS running FROM invocations WHERE status = 'running'"
    ) or {}
    return {"running_invocations": int(decisions.get("running") or 0)}


def assert_within_limits(principal: AgentPrincipal, capability_id: str, provider_id: str | None) -> None:
    """Raise 429 when a token/capability limit is already reached.

    Defect fixed: `LimitDecision.retry_after_seconds` was computed and then
    dropped, so the 429 body told the caller *that* it was throttled but not for
    how long — CONTRACT §6 requires `rate_limited` to be actionable and the login
    path already ships `retry_after_seconds`. Keep it in `details` for both the
    agent and the capability decisions.
    """

    def _details(decision: LimitDecision) -> dict[str, Any]:
        details = dict(decision.details or {})
        if decision.retry_after_seconds:
            details["retry_after_seconds"] = int(decision.retry_after_seconds)
        return details

    agent_decision = check_agent_limits(principal)
    if not agent_decision.allowed:
        raise ApiError(
            agent_decision.code,
            agent_decision.message,
            status_code=429,
            details=_details(agent_decision),
            actions=list(agent_decision.actions),
        )
    cap_decision = check_capability_limits(capability_id, provider_id)
    if not cap_decision.allowed:
        raise ApiError(
            cap_decision.code,
            cap_decision.message,
            status_code=429,
            details=_details(cap_decision),
            actions=list(cap_decision.actions),
        )
