"""Shape C — orchestrator plus read-only investigators. Student work.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.

An LLM summary of the parent context is not a sanitizer. Use the allowlist.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.handoff import HandoffError, make_handoff
from lab8.mock_llm import complete
from lab8.runtime import ToolRejected, run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import compute_compensation, get_claim_view, tool_cost
from lab8.trace import Trace

SHAPE = "orchestrator"
INVESTIGATOR_TASK = "investigate_order_and_refund_history"
# Read-only tools only. compute_compensation stays with the orchestrator: money is code, not a worker.
INVESTIGATOR_TOOLS = ["get_claim_view", "lookup_order", "lookup_refund_history"]


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


def build_investigator_handoff(request_id: str, claim_id: str, facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Handoff for the investigator: allowlisted fields, allowlisted facts, read-only tools.

    The parent context (ticket, principal id, parent_secret, headers) is never passed in.
    """
    return make_handoff(
        request_id=request_id,
        claim_id=claim_id,
        task=INVESTIGATOR_TASK,
        facts=dict(facts or {}),
        allowed_tools=list(INVESTIGATOR_TOOLS),
    )


def _run(conn: sqlite3.Connection, claim_id: str, *, seed: int, crash_at: str | None) -> RunResult:
    budget = Budget()
    run_id = make_run_id(SHAPE, claim_id, seed)
    trace = Trace(run_id)

    def done(status: str, proposal: int, reason: str | None) -> RunResult:
        # The model only drafts the reply. It never sees ticket, order row or amount.
        complete(budget, seed=seed, purpose="draft", prompt=f"status={status}")
        trace.add("decision", {"status": status, "proposal_amount": int(proposal), "reason": reason})
        return finish(
            conn,
            shape=SHAPE,
            claim_id=claim_id,
            seed=seed,
            status=status,
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
            error_reason=reason,
        )

    # Orchestrator reads the allowlisted claim view (no SELECT * on claims).
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    if not claim["found"]:
        trace.add("claim", {"found": False})
        return done("failed", 0, "unknown_claim")
    # Ticket is user data: not traced, not parsed, not forwarded to workers.
    trace.add("claim", {"found": True, "has_order_id": bool(claim["order_id"])})
    if not claim["order_id"]:
        return done("needs_info", 0, "missing_order_id")

    # Step 1: allowlisted handoff with read-only tools. Planning call sees only the task name.
    complete(budget, seed=seed, purpose="plan", prompt=f"task={INVESTIGATOR_TASK}")
    try:
        handoff = build_investigator_handoff(run_id, claim_id)
    except HandoffError as exc:
        trace.add("handoff_rejected", {"reason": str(exc)})
        return done("failed", 0, "handoff_rejected")
    trace.add("handoff", safe_dump(handoff))

    # Step 2: investigators run only the read tools named in the handoff.
    try:
        report = run_readonly_investigators(conn, budget, handoff, claim["principal_id"])
    except ToolRejected as exc:
        trace.add("investigator_rejected", {"tool": str(exc)})
        return done("failed", 0, "tool_rejected")
    tools = report["tools"]

    order_view = tools.get("lookup_order") or {}
    if not order_view.get("visible"):
        trace.add("lookup_order", {"visible": False, "reason": order_view.get("reason")})
        return done("escalated", 0, order_view.get("reason") or "order_not_visible")
    order = order_view["order"]  # lookup_order already dropped phone, address, item, note

    # Investigator report goes back through the same allowlist (FACT_KEYS) before tracing.
    findings = build_investigator_handoff(run_id, claim_id, facts={**order, "order_status": order.get("status")})
    history = tools.get("lookup_refund_history") or {}
    trace.add(
        "investigator_report",
        {
            "facts": safe_dump(findings["facts"]),
            "refund_count": int(history.get("refund_count", 0)),
            "refunded_vnd": int(history.get("refunded_vnd", 0)),
        },
    )

    # Step 3: the orchestrator (code) quotes from the order view.
    budget.add_tool(tool_cost())
    quote = compute_compensation(conn, order)
    proposal = int(quote["amount_vnd"])
    trace.add("quote", {"amount_vnd": proposal, "reason": quote["reason"], "clause_ids": list(quote["clause_ids"])})
    if proposal <= 0:
        return done("denied", 0, quote["reason"])

    # Full quote to the host. Host checks approval, per-order cap, idempotency.
    # InjectedCrash is not swallowed: flush the trace and re-raise.
    budget.add_tool(tool_cost())
    try:
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
    except InjectedCrash:
        trace.add("crash", {"at": crash_at})
        trace.write()
        raise
    trace.add("issue_refund", {"ok": outcome["ok"], "reason": outcome["reason"], "conflict": outcome["conflict"]})

    if not outcome["ok"]:
        return done("failed" if outcome["conflict"] else "denied", proposal, outcome["reason"])
    return done("resolved", proposal, None)