"""Per-workspace status report.

Generates a multi-tab Excel workbook with one tab per workspace
(Cardiowell, Taiga, Edge) plus a Summary tab. Designed to be
runnable on-demand by an operator without needing to touch Mongo
directly.

Metrics per workspace:
  * Comments sent           — manual_comment_jobs.status="posted"
  * Replies received        — unique authors in
                              latest_my_comment_replies_thread on those
                              jobs (excluding our own outgoing reply
                              comment_ids)
  * Connection requests sent — linkedin_invitations rows in
                              {queued, sent, accepted} (excludes
                              dry_run + failed)
  * Connections accepted    — linkedin_invitations.status="accepted",
                              PLUS reconciled-from-tracker count
                              (people in the tracker's new_relation
                              events even if our DB row is failed)
  * DMs sent                — linkedin_dms.status="sent"
  * Inbound DMs received    — from the tracker CSV (optional input;
                              if omitted we skip this column)
  * Meeting-accept signals  — inbound DMs whose text matches a positive
                              acceptance regex ("happy to meet", "yes
                              absolutely", etc.) — heuristic

Reproducibility: run from inside the backend container as:

    docker exec -e TRACKER_CSV=/tmp/four_accts_tracker.csv \\
        infra-backend-1 sh -c \\
        "PYTHONPATH=/app python backend/scripts/workspace_status_report.py"

Or skip the tracker if you don't have one — the script will still
produce all the metrics that don't depend on inbound DM data.

The workspace → accounts mapping below is the SOURCE OF TRUTH for
which Unipile account_ids belong to which workspace. Bring new
accounts online by adding them here; the rest of the script reads
this dict.
"""
from __future__ import annotations

import asyncio
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

# Make sure the `app` package is importable when run from any cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

import openpyxl  # noqa: E402
from openpyxl.styles import Font, PatternFill, Alignment  # noqa: E402

from app.database import connect_to_mongo, get_db  # noqa: E402


# ── Workspace → Unipile account_id mapping ──────────────────────────────
# Source of truth. Updating here propagates through every count.
WORKSPACES: dict[str, dict[str, str]] = {
    "Cardiowell": {
        "5LEWeqL_RoiHQOPxIIuiFw": "Yair Lurie",
    },
    "Taiga": {
        "a7FvzhdtRCiSSIfJ_pYQDg": "Nanda Guntupalli",
        "c6G_f6R7Q2-E6db4WVoHPw": "Adam Wax",
    },
    "Edge": {
        "lW0YMCIWQtikxOWPPixUBA": "Danny Vega",
        "4Qrvg56sQMi2N8aA-9Mchw": "Nick Castelli",
    },
}

# Meeting-accept heuristic — case-insensitive substring match. Picked
# from real positive replies we've already received (Carri "Yes,
# absolutely!"; Seleipiri "I am happy to meet up"). Conservative; tweak
# in the operator's REPL if false positives appear.
ACCEPT_PATTERNS = [
    r"\bhappy to meet\b",
    r"\byes,?\s*absolutely\b",
    r"\bsounds good\b",
    r"\bsounds great\b",
    r"\blet'?s\s+(set|schedule|book)\b",
    r"\bcalendar\b",
    r"\bcalendly\b",
    r"\bbook a (call|meeting|time)\b",
    r"\bschedule a (call|meeting|time)\b",
    r"\bset up a (call|time|meeting)\b",
    r"\bworks for me\b",
    r"\bcan we (chat|talk|catch up)\b",
    r"\bI'?m in\b",
    r"\bI'?d love to\b",
]
ACCEPT_RX = re.compile("|".join(ACCEPT_PATTERNS), re.IGNORECASE)


# ── Tracker parser ──────────────────────────────────────────────────────
def parse_tracker(path: str | None) -> dict[str, list[dict[str, str]]]:
    """Read the Unipile-webhook tracker CSV (optional). Returns a dict
    {account_id: [rows]} keyed by sending account."""
    if not path or not os.path.exists(path):
        return {}
    by_account: dict[str, list[dict[str, str]]] = defaultdict(list)
    with open(path) as f:
        for row in csv.DictReader(f):
            aid = (row.get("account_id") or "").strip()
            if aid:
                by_account[aid].append(row)
    return dict(by_account)


def matches_acceptance(text: str) -> bool:
    if not text:
        return False
    return bool(ACCEPT_RX.search(text))


# ── Workspace metrics ──────────────────────────────────────────────────
async def workspace_metrics(
    db, workspace: str, accounts: dict[str, str], tracker: dict[str, list]
) -> dict[str, Any]:
    """Compute every metric for a single workspace."""
    account_ids = list(accounts.keys())

    # 1. Comments sent
    comments_sent = await db.manual_comment_jobs.count_documents({
        "unipile_account_id": {"$in": account_ids},
        "status": "posted",
    })

    # 2. Replies received — unique authors across captured threads,
    #    excluding our own outgoing reply comment_ids.
    reply_authors: set[str] = set()
    async for j in db.manual_comment_jobs.find({
        "unipile_account_id": {"$in": account_ids},
        "status": "posted",
        "latest_my_comment_replies_thread.0": {"$exists": True},
    }):
        outgoing_cids = {
            (o.get("comment_id") or "")
            for o in (j.get("my_outgoing_replies") or [])
            if o.get("comment_id")
        }
        for r in j.get("latest_my_comment_replies_thread") or []:
            if (r.get("comment_id") or "") in outgoing_cids:
                continue
            pid = r.get("author_provider_id") or r.get("author_name") or ""
            if pid:
                reply_authors.add(pid)
    replies_received = len(reply_authors)

    # 3. Connection requests sent — anything not in {dry_run}.
    #    "failed" rows are INCLUDED because a 422 already_invited_recently
    #    proves an invite WAS sent (just from a different channel).
    invites_sent = await db.linkedin_invitations.count_documents({
        "sent_via_account_id": {"$in": account_ids},
        "status": {"$nin": ["dry_run"]},
    })

    # 4. Connections accepted. Take the union of:
    #      a) DB rows with status=accepted
    #      b) DB rows that 422'd but the tracker shows new_relation
    accepted_urns_db: set[str] = set()
    async for inv in db.linkedin_invitations.find({
        "sent_via_account_id": {"$in": account_ids},
        "status": "accepted",
    }):
        accepted_urns_db.add(inv.get("target_provider_id") or "")

    # Tracker-side: any URN in the tracker with event_type=new_relation
    # for one of this workspace's accounts.
    accepted_urns_tracker: set[str] = set()
    for aid in account_ids:
        for row in tracker.get(aid, []):
            if row.get("event_type") == "new_relation":
                urn = (row.get("counterparty_urn") or "").strip()
                if urn:
                    accepted_urns_tracker.add(urn)
    # Only count tracker URNs we ACTUALLY tried to invite (filter against
    # our linkedin_invitations table) — otherwise we count all 50+
    # historic 1st-degree connections the account had before we started.
    invited_urns: set[str] = set()
    async for inv in db.linkedin_invitations.find({
        "sent_via_account_id": {"$in": account_ids},
    }):
        invited_urns.add(inv.get("target_provider_id") or "")
    accepted_urns_tracker &= invited_urns
    connections_accepted = len(accepted_urns_db | accepted_urns_tracker)

    # 5. DMs sent via our endpoint
    dms_sent_endpoint = await db.linkedin_dms.count_documents({
        "sent_via_account_id": {"$in": account_ids},
        "status": "sent",
    })
    # DMs sent OUTSIDE the endpoint, captured by the tracker
    dms_sent_external = 0
    for aid in account_ids:
        for row in tracker.get(aid, []):
            if (
                row.get("event_type") == "message_received"
                and row.get("direction") == "OUT"
            ):
                dms_sent_external += 1
    # NB: external count includes ALL outbound messages, not just
    # first-touch DMs. Operator should interpret accordingly.

    # 6. Inbound DMs received (from tracker) + 7. meeting-accept signals
    inbound_count = 0
    meeting_accepts: list[tuple[str, str]] = []  # (sender_name, snippet)
    for aid in account_ids:
        for row in tracker.get(aid, []):
            if (
                row.get("event_type") == "message_received"
                and row.get("direction") == "IN"
            ):
                inbound_count += 1
                msg = row.get("message") or ""
                if matches_acceptance(msg):
                    meeting_accepts.append(
                        (row.get("counterparty_name") or "?", msg[:140])
                    )

    return {
        "workspace": workspace,
        "accounts": [
            {"display_name": name, "account_id": aid}
            for aid, name in accounts.items()
        ],
        "comments_sent": comments_sent,
        "replies_received": replies_received,
        "connection_requests_sent": invites_sent,
        "connections_accepted": connections_accepted,
        "dms_sent_via_endpoint": dms_sent_endpoint,
        "dms_sent_external_total": dms_sent_external,
        "inbound_dms_received": inbound_count,
        "meeting_accept_signals": len(meeting_accepts),
        "meeting_accept_examples": meeting_accepts[:10],
    }


# ── Excel formatting ────────────────────────────────────────────────────
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
LABEL_FONT = Font(bold=True)
NUMBER_FONT = Font(size=14)


def write_workbook(report: list[dict[str, Any]], output_path: str) -> None:
    wb = openpyxl.Workbook()

    # Summary tab
    ws = wb.active
    ws.title = "Summary"
    headers = [
        "Workspace",
        "Comments sent",
        "Replies received",
        "Connection requests sent",
        "Connections accepted",
        "DMs sent (endpoint)",
        "DMs sent (incl external)",
        "Inbound DMs received",
        "Meeting-accept signals",
    ]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32

    for i, row in enumerate(report, start=2):
        ws.cell(row=i, column=1, value=row["workspace"]).font = LABEL_FONT
        ws.cell(row=i, column=2, value=row["comments_sent"])
        ws.cell(row=i, column=3, value=row["replies_received"])
        ws.cell(row=i, column=4, value=row["connection_requests_sent"])
        ws.cell(row=i, column=5, value=row["connections_accepted"])
        ws.cell(row=i, column=6, value=row["dms_sent_via_endpoint"])
        ws.cell(row=i, column=7, value=row["dms_sent_external_total"])
        ws.cell(row=i, column=8, value=row["inbound_dms_received"])
        ws.cell(row=i, column=9, value=row["meeting_accept_signals"])

    # Generated-at footer
    footer_row = 2 + len(report) + 1
    ws.cell(row=footer_row, column=1, value=f"Generated: {datetime.utcnow().isoformat()}Z")
    ws.cell(row=footer_row, column=1).font = Font(italic=True, color="888888", size=9)

    # Column widths
    for col_idx, w in enumerate([18, 14, 14, 20, 18, 18, 22, 18, 22], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = w

    # One tab per workspace
    for row in report:
        ws = wb.create_sheet(row["workspace"])
        ws["A1"] = row["workspace"]
        ws["A1"].font = Font(bold=True, size=18, color="1F4E79")
        ws["A2"] = "Accounts in this workspace:"
        ws["A2"].font = LABEL_FONT
        for i, a in enumerate(row["accounts"], start=3):
            ws.cell(row=i, column=1, value=f"  {a['display_name']}")
            ws.cell(row=i, column=2, value=a["account_id"])
            ws.cell(row=i, column=2).font = Font(name="Courier", size=9, color="888888")

        # Metrics block
        next_row = 3 + len(row["accounts"]) + 1
        labels = [
            ("Comments sent", row["comments_sent"]),
            ("Replies received", row["replies_received"]),
            ("Connection requests sent", row["connection_requests_sent"]),
            ("Connections accepted", row["connections_accepted"]),
            ("DMs sent (via endpoint)", row["dms_sent_via_endpoint"]),
            ("DMs sent (incl external outbound from tracker)",
             row["dms_sent_external_total"]),
            ("Inbound DMs received", row["inbound_dms_received"]),
            ("Meeting-accept signals (heuristic match)",
             row["meeting_accept_signals"]),
        ]
        for i, (k, v) in enumerate(labels):
            ws.cell(row=next_row + i, column=1, value=k).font = LABEL_FONT
            cell = ws.cell(row=next_row + i, column=2, value=v)
            cell.font = NUMBER_FONT
            cell.alignment = Alignment(horizontal="right")

        # Meeting-accept examples
        if row["meeting_accept_examples"]:
            ex_start = next_row + len(labels) + 2
            ws.cell(row=ex_start, column=1, value="Meeting-accept signal examples:").font = LABEL_FONT
            for i, (name, snippet) in enumerate(row["meeting_accept_examples"]):
                ws.cell(row=ex_start + 1 + i, column=1, value=f"  · {name}")
                ws.cell(row=ex_start + 1 + i, column=2, value=snippet)

        ws.column_dimensions["A"].width = 48
        ws.column_dimensions["B"].width = 80

    wb.save(output_path)
    print(f"# wrote workbook to {output_path}")


# ── Entry point ─────────────────────────────────────────────────────────
async def main():
    tracker_path = os.environ.get("TRACKER_CSV") or (sys.argv[1] if len(sys.argv) > 1 else None)
    output_path = (
        os.environ.get("OUTPUT_PATH")
        or (sys.argv[2] if len(sys.argv) > 2 else f"/tmp/workspace_status_{datetime.utcnow().strftime('%Y-%m-%d')}.xlsx")
    )

    tracker = parse_tracker(tracker_path)
    if tracker:
        print(f"# loaded tracker from {tracker_path} ({sum(len(v) for v in tracker.values())} rows)")
    else:
        print("# no tracker CSV provided (TRACKER_CSV env or argv[1]) — "
              "inbound-DM + external-OUT counts will be 0")

    await connect_to_mongo()
    db = get_db()

    report = []
    for workspace, accounts in WORKSPACES.items():
        m = await workspace_metrics(db, workspace, accounts, tracker)
        report.append(m)
        print(
            f"  {workspace:12}  comments={m['comments_sent']:3}  "
            f"replies={m['replies_received']:3}  "
            f"invites={m['connection_requests_sent']:3}  "
            f"accepted={m['connections_accepted']:3}  "
            f"DMs_endpoint={m['dms_sent_via_endpoint']:3}  "
            f"DMs_total_out={m['dms_sent_external_total']:3}  "
            f"inbound={m['inbound_dms_received']:3}  "
            f"meeting_accepts={m['meeting_accept_signals']:3}"
        )

    write_workbook(report, output_path)


if __name__ == "__main__":
    asyncio.run(main())
