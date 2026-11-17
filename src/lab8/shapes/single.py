from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import ToolRejected, run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import compute_compensation, tool_cost
from lab8.trace import Trace

SHAPE = "single"
MAX_STEPS = 8


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _make_decide(claim_id: str, seed: int, budget: Budget, memory: dict[str, Any], trace: Trace):
    """The agent's policy. It only picks READ tools and when to stop. Never money, never writes."""

    def decide(seen: dict[str, Any]) -> dict[str, Any]:
        if "last_tool" in seen:
            memory[seen["last_tool"]] = seen["last_result"]
        complete(budget, seed=seed, purpose="react", prompt=f"claim={claim_id} step={len(memory)}")

        if "get_claim_view" not in memory:
            action = {"tool": "get_claim_view", "args": {"claim_id": claim_id}}
        else:
            claim = memory["get_claim_view"]
            if not claim.get("found"):
                action = {"final": {"status": "failed", "reason": "unknown_claim"}}
            elif not claim.get("order_id"):
                action = {"final": {"status": "needs_info", "reason": "missing_order_id"}}
            elif "lookup_order" not in memory:
                action = {"tool": "lookup_order",
                          "args": {"order_id": claim["order_id"], "principal_id": claim["principal_id"]}}
            elif not memory["lookup_order"].get("visible"):
                action = {"final": {"status": "escalated", "reason": memory["lookup_order"].get("reason")}}
            elif "lookup_refund_history" not in memory:
                action = {"tool": "lookup_refund_history",
                          "args": {"order_id": claim["order_id"], "principal_id": claim["principal_id"]}}
            else:
                action = {"final": {"status": "ready_to_quote", "reason": None}}

        trace.add("react_step", {"step": len(memory) + 1,
                                 "tool": action.get("tool"),
                                 "final": (action.get("final") or {}).get("status")})
        return action

    return decide


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    trace = Trace(make_run_id(SHAPE, claim_id, seed))
    memory: dict[str, Any] = {}

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status}")
        trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
        return finish(conn, shape=SHAPE, claim_id=claim_id, seed=seed, status=status,
                      proposal_amount=proposal, budget=budget, trace=trace, error_reason=reason)

    try:
        outcome = run_react(conn, budget, observation={"claim_id": claim_id},
                            decide=_make_decide(claim_id, seed, budget, memory, trace),
                            max_steps=MAX_STEPS)
    except ToolRejected as exc:
        return done("failed", 0, f"tool_rejected:{exc}")

    if outcome["stopped"] == "step_cap":
        return done("failed", 0, "step_cap")
    final = outcome["final"]
    if final["status"] != "ready_to_quote":
        return done(final["status"], 0, final.get("reason"))

    history = memory["lookup_refund_history"]
    trace.add("refund_history", {"refund_count": history.get("refund_count", 0),
                                 "refunded_vnd": history.get("refunded_vnd", 0)})

    order = memory["lookup_order"]["order"]
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", {"amount_vnd": proposal, "reason": quote["reason"], "clause_ids": list(quote["clause_ids"])})

    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    budget.add_tool(tool_cost())
    try:
        result = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    trace.add("issue_refund", {"ok": result["ok"], "reason": result["reason"], "conflict": result["conflict"]})

    if not result["ok"]:
        return done("failed" if result["conflict"] else "denied", proposal, result["reason"])
    return done("resolved", proposal, None)
