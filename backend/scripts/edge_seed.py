"""Restore Edge as the active client on the nicolasvilaalarcon02@gmail.com
operator.

Reverses what cardiowell_seed.py did:
  - Flips users.client_slug from "cardiowell" → "edge".
  - Rewrites product_description + product_extracted (target_titles /
    industries / geographies / pain_points / suggested_keywords) to the
    Edge persona: HR / RCM / Operations leaders at health systems,
    hospital groups, medical groups, FQHCs.
  - Clears the operator's stored icp_rubric so configs/edge/icp_rubric.json
    is the single source of truth at runtime (loaded via
    services.client_config.for_operator). If you want to override per-axis,
    set users.icp_rubric to the override dict; merge_icp_rubric() fills
    missing axes from the client rubric.
  - Restores the cofounder identity (display name + voice profile).
  - Keeps the existing Unipile account binding (KW0q...) so the worker can
    run immediately — change it manually if you need a different account.

Idempotent. Run inside the backend container:
    docker exec -e PYTHONPATH=/app infra-backend-1 python /app/scripts/edge_seed.py
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings


OPERATOR_EMAIL = "nicolasvilaalarcon02@gmail.com"
TARGET_CLIENT_SLUG = "edge"


# ─── Edge product description ─────────────────────────────────────────────
# Pulled from configs/edge/client.json + the historical operator narrative.
PRODUCT_DESCRIPTION = """Edge is a healthcare-delivery-side workforce + operations + revenue-cycle optimization platform sold INTO health systems, hospital groups, medical groups, and FQHCs.

Buyer profile: HR / Talent Acquisition / Revenue Cycle Management / Operations leaders at care-delivery organizations. NOT pharma or biotech commercial teams — Edge sits on the provider side of the table.

Three product lanes:
  1. HR / Workforce — nurse turnover, RIF paradox, hiring freezes, workforce planning under margin pressure.
  2. RCM — denied claims rate, front-end denial prevention, patient access denials.
  3. Operations — health system efficiency, hospital labor cost pressure, ambulatory + FQHC operations.

Buyers are typically CHRO / VP HR / VP Talent Acquisition (HR lane), VP Revenue Cycle / Director RCM / Director Patient Access (RCM lane), COO / VP Operations / VP Workforce Planning / CFO (Operations + Finance lane). Geography is US-only; sweet spots are large hospital networks, multi-site medical groups, and FQHCs running through HRSA quality reporting cycles.

Strategy on comments: validate operational pain (workforce shortage, denial rates, margin compression) and route conversation toward Edge's lane-specific solve. Avoid pharma/biotech commercial framing — Edge sells against operational and financial reality, not therapy-area economics.
"""


# Synthesized from configs/edge/icp_rubric.json title tiers + the strategy
# note above. Same field names the engine reads in discovery + gates.
PRODUCT_EXTRACTED = {
    "target_titles": [
        # HR / Workforce lane (Edge tier_1 buyer)
        "Chief Human Resources Officer", "CHRO",
        "VP Human Resources", "VP HR",
        "VP Talent Acquisition", "VP TA",
        "Chief People Officer", "CPO",
        "Director Human Resources", "Director HR",
        "Director Talent Acquisition", "Director TA",
        "Director People",
        "VP Workforce Planning", "Director Workforce",
        "Manager Human Resources",
        "Workforce Planning Manager",
        # RCM lane
        "VP Revenue Cycle", "VP RCM",
        "Director Revenue Cycle Management", "Director RCM",
        "Director Patient Access",
        "Senior Manager RCM", "RCM Specialist Senior",
        "Billing Director",
        # Operations / Finance lane
        "Chief Operating Officer", "COO",
        "VP Operations",
        "Director Operations",
        "Director Practice Operations",
        "Chief Financial Officer", "CFO",
        "VP Finance",
        "Senior Manager Operations",
    ],
    "target_industries": [
        "Hospitals and Health Care",
        "Hospital and Health Care",
        "Health Systems",
        "Hospital Network",
        "Medical Groups",
        "Medical Group Practice",
        "Federally Qualified Health Centers", "FQHC",
        "Community Health Centers",
        "Physician Groups",
        "Accountable Care Organization", "ACO",
        "Management Services Organization", "MSO",
    ],
    "target_geographies": [
        "United States", "US", "USA",
    ],
    "target_pain_points": [
        "Nurse turnover and hospital workforce shortage driving operational instability",
        "RIF paradox: layoffs at the C-suite level while frontline clinical roles remain understaffed",
        "Healthcare hiring freezes interrupting backfill of critical clinical and operations roles",
        "RCM efficiency gaps and rising denial rates eroding hospital margins",
        "Front-end RCM denials caused by patient-access workflow breakdowns",
        "Health system financial pressure under shifting payer mix and 2026 CMS rules",
        "Hospital margin compression squeezing labor and ops budgets simultaneously",
        "FQHC operational efficiency under HRSA quality reporting cycles",
        "Ambulatory operations efficiency at multi-site medical groups",
        "Hospital labor cost pressure as a % of NPR",
        "Workforce planning under sustained nursing vacancy",
    ],
    "suggested_keywords": {
        "tier_1": [
            "hospital workforce shortage",
            "RIF paradox health system",
            "healthcare hiring freeze",
            "RCM efficiency hospital",
            "denied claims rate hospital",
            "patient access denials",
            "health system efficiency",
            "hospital labor cost pressure",
            "front-end RCM denials",
            "nurse turnover rate hospital",
        ],
        "tier_2": [
            "health system financial pressure",
            "hospital margin compression",
            "FQHC operational efficiency",
            "medical group operations",
            "nursing workforce planning",
            "ambulatory operations efficiency",
        ],
        "tier_3": [
            "hospital operations",
            "health system management",
        ],
    },
}


# Operator-level schedule + quotas. Restored to the historical Edge values
# (12 daily target, 6 hard floor — softer than Cardiowell's 60/20 because
# Edge has a tighter ICP and lower volume).
DAILY_TARGET = 12
HARD_FLOOR = 6
RUN_TIME_LOCAL = "09:00"
COMMENT_QUOTAS = {
    "A": [35, 40],
    "B": [22, 25],
    "C": [14, 16],
    "D": [9, 12],
    "E": [7, 10],
    "F": [0, 5],
}


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    line("═")
    print(f"Edge restore  →  operator={OPERATOR_EMAIL}")
    line("═")

    op = await db.users.find_one({"email": OPERATOR_EMAIL})
    if not op:
        print(f"FATAL: operator {OPERATOR_EMAIL} not found")
        return
    op_id = op["_id"]
    print(
        f"[step 1] operator_id={op_id}  was client_slug={op.get('client_slug')!r}  "
        f"paused={op.get('paused')}"
    )

    now = datetime.now(timezone.utc)
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "client_slug": TARGET_CLIENT_SLUG,
                "product_description": PRODUCT_DESCRIPTION,
                "product_extracted": PRODUCT_EXTRACTED,
                "paused": False,
                "onboarding_complete": True,
                "run_time_local": RUN_TIME_LOCAL,
                "daily_target": DAILY_TARGET,
                "hard_floor": HARD_FLOOR,
                "comment_quotas": COMMENT_QUOTAS,
                "updated_at": now,
            },
            # Clear any operator-level icp_rubric override so the runtime
            # falls through to configs/edge/icp_rubric.json. If a future
            # operator needs to override one axis, set users.icp_rubric =
            # {axis_name: {...}} and merge_icp_rubric() will fill in the
            # rest from the client config.
            "$unset": {"icp_rubric": ""},
        },
    )
    print(
        f"[step 2] users.{op_id}: client_slug=edge, paused=False, "
        f"product_extracted={{titles={len(PRODUCT_EXTRACTED['target_titles'])}, "
        f"industries={len(PRODUCT_EXTRACTED['target_industries'])}, "
        f"geos={len(PRODUCT_EXTRACTED['target_geographies'])}, "
        f"pain={len(PRODUCT_EXTRACTED['target_pain_points'])}, "
        f"kw_t1={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_1'])}, "
        f"kw_t2={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_2'])}, "
        f"kw_t3={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_3'])}}}"
    )
    print("[step 2] users.icp_rubric cleared — configs/edge/icp_rubric.json is canonical")

    # Restore cofounder identity. Keep the Unipile account binding (changing
    # it here would mask any operator-side reconnect work). The display name
    # + voice profile flip is what matters for the prompt builder.
    cf = await db.cofounders.find_one({"operator_id": op_id, "active": True})
    if not cf:
        # Try inactive too in case it was deactivated earlier
        cf = await db.cofounders.find_one({"operator_id": op_id})
    if not cf:
        print("WARN: no cofounder found for this operator")
        return

    # Build a minimal Edge voice profile from configs/edge/voice_profiles.json
    edge_voice_path = Path("/configs/edge/voice_profiles.json")
    edge_voice_notes: list[str] = []
    edge_voice_avoided: list[str] = []
    if edge_voice_path.exists():
        vp = json.loads(edge_voice_path.read_text())
        edge_voice_notes = vp.get("shared_voice_notes") or []
        edge_voice_avoided = vp.get("avoided_phrases") or []

    edge_voice = {
        "tone_description": (
            "Edge rep voice. Healthcare-delivery operator framing — talk to "
            "HR / RCM / Operations leaders the way a peer would: workforce "
            "shortages, denial rates, labor cost as % of NPR, margin "
            "compression, FQHC HRSA cycles. Specific numbers always. No "
            "pharma/biotech commercial language — Edge sits on the provider "
            "side. Use plain sentences, no em-dashes, no bullets, end with a "
            "question when possible."
        ),
        "shared_voice_notes": edge_voice_notes,
        "preferred_close_patterns": ["question", "data_offer"],
        "avoided_phrases": edge_voice_avoided,
        "source_a_template": "",
        "source_b_template": "",
        "examples": [],
    }

    await db.cofounders.update_one(
        {"_id": cf["_id"]},
        {
            "$set": {
                "display_name": "Alex Gregoriades (Edge)",
                "voice_profile": edge_voice,
                "active": True,
                "updated_at": now,
            }
        },
    )
    print(
        f"[step 3] cofounder {cf['_id']}: display_name=Alex Gregoriades (Edge), "
        f"unipile_account_id={cf.get('unipile_account_id')!r} (unchanged), "
        f"voice_profile=edge"
    )

    # Clear stale industry-id cache so the next RULE 24 run resolves Edge's
    # industries fresh against LinkedIn (different industry set from
    # Cardiowell — health systems, FQHC, medical groups vs cardiology/etc).
    deleted = await db.unipile_industry_id_cache.delete_one({"operator_id": op_id})
    print(f"[step 4] cleared {deleted.deleted_count} stale unipile_industry_id_cache entry")

    line()
    op_after = await db.users.find_one({"_id": op_id})
    cf_after = await db.cofounders.find_one({"_id": cf["_id"]})
    print(
        f"Edge operator: slug={op_after.get('client_slug')!r}  paused={op_after.get('paused')}"
    )
    print(
        f"Cofounder: name={cf_after.get('display_name')!r}  "
        f"unipile={cf_after.get('unipile_account_id')!r}  active={cf_after.get('active')}"
    )
    print(f"Operator id (for run-now): {op_id}")
    print(f"Cofounder id: {cf['_id']}")
    line("═")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
