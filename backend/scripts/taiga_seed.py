"""Taiga client seed — operator + 1 cofounder + ICP rubric (independent-physician focus).

Loads the Taiga rubric verbatim from configs/taiga/ instead of relying on the
LLM extractor (deterministic + faster). Discovers a connected Unipile account
on the configured tenant and attaches it to the cofounder.

Run inside the backend container:
    docker exec infra-backend-1 python /app/scripts/taiga_seed.py
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from app.auth.jwt import hash_password
from app.config import settings
from app.services.unipile import list_accounts


import os

OPERATOR_EMAIL = os.environ.get("SEED_EMAIL", "nicolas@glnkco.com")
OPERATOR_NAME = os.environ.get("SEED_NAME", "Nicolas (Taiga ops)")
OPERATOR_PASSWORD = os.environ.get("SEED_PASSWORD", "TaigaDev2026!")
SLATE_RECIPIENT = os.environ.get("SEED_SLATE_RECIPIENT", OPERATOR_EMAIL)

TAIGA_CONFIG_DIR = Path("/configs/taiga")


PITCH = """Taiga is a billing and revenue-cycle platform for independent medical practices and small physician groups. We focus on the specialties where reimbursement is hardest — psychiatry (50%+ mental-health denial rates), dermatology, podiatry, cardiology, primary care — and where one bad month of billing can shut a solo practice down.

Customers use Taiga to: cut their claim denial rate (independent practices routinely run 25-40% denial rates vs 5-10% at scale); shrink prior-authorization turnaround from 14 days to 2; enforce mental-health parity rules that payers routinely violate; and run E&M coding correctly without losing money to undercoding (the 99213/99214 trap costs the average practice $40-80K/year); and replace fragile billing staff dependence with software that doesn't quit.

Buyers are: physician owners running solo or small group practices (psychiatry, dermatology, podiatry, primary care, cardiology, internal medicine), practice administrators, practice managers, office managers, billing managers, RCM directors, directors of billing. Geographic focus is United States with priority in Florida, Texas, and Georgia. ACVs run from $5K (single-provider solo) to $80K+ (multi-site practice).

The wedge: most practice owners are still arguing with their billing service or their EHR's built-in claims module — neither of which understands the specialty-specific rules that drive 60%+ of their denials.
"""


def load_taiga_configs() -> tuple[dict, dict]:
    rubric = json.loads((TAIGA_CONFIG_DIR / "icp_rubric.json").read_text())
    keywords = json.loads((TAIGA_CONFIG_DIR / "keyword_pools.json").read_text())
    return rubric, keywords


def build_product_extracted(rubric: dict, keywords: dict) -> dict:
    """Manually shape the product_extracted blob the engine expects, matching
    the schema icp_extractor.extract returns. Pulls titles from rubric tiers,
    keywords from keyword_pools, and adds geo + pain points by hand."""
    # Flatten titles by tier
    title_tiers = rubric["title"]["tiers"]
    target_titles_dict = {
        "tier_1": title_tiers[0]["matches"] if len(title_tiers) > 0 else [],
        "tier_2": title_tiers[1]["matches"] if len(title_tiers) > 1 else [],
        "tier_3": title_tiers[2]["matches"] if len(title_tiers) > 2 else [],
    }
    target_titles_flat = (
        target_titles_dict["tier_1"]
        + target_titles_dict["tier_2"]
        + target_titles_dict["tier_3"]
    )

    industries = (rubric.get("industry") or {}).get("tiers", [{}])[0].get("matches", [])

    suggested = {
        "tier_1": keywords.get("tier_1_topical", []),
        "tier_2": keywords.get("tier_2_topical", []),
        "tier_3": keywords.get("tier_3_topical", []),
    }

    return {
        "target_industries": industries,
        "target_titles": target_titles_flat,
        "target_geographies": ["United States", "Florida", "Texas", "Georgia"],
        "target_pain_points": [
            "claim denials",
            "prior authorization burden",
            "mental health parity violations",
            "E&M undercoding",
            "billing staff fragility",
            "reimbursement delays",
        ],
        "suggested_keywords": suggested,
    }


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    line("═")
    print(f"TAIGA seed  email={OPERATOR_EMAIL}")
    line("═")

    # 0. Optionally pick a Unipile account (set SEED_UNIPILE_ACCOUNT_ID, or leave
    #    unset to skip auto-attach — user will connect via /settings).
    print("[step 0] Resolving Unipile account_id for cofounder...")
    unipile_account_id: str | None = os.environ.get("SEED_UNIPILE_ACCOUNT_ID")
    if unipile_account_id:
        print(f"[step 0] using SEED_UNIPILE_ACCOUNT_ID={unipile_account_id}")
    elif os.environ.get("SEED_SKIP_UNIPILE", "1") == "1":
        print("[step 0] SEED_SKIP_UNIPILE=1 (default) — cofounder will be created "
              "without an account_id. Connect a real LinkedIn at /settings.")
        try:
            accounts = list_accounts()
            if accounts:
                print(f"[step 0] FYI {len(accounts)} accounts available on the tenant; "
                      "set SEED_UNIPILE_ACCOUNT_ID=<id> to auto-attach one.")
                for a in accounts[:5]:
                    print(f"          - id={a.id}  name={a.name}  type={a.account_type}")
        except Exception as err:
            print(f"[step 0] (list_accounts failed: {err})")
    else:
        try:
            accounts = list_accounts()
            if accounts:
                unipile_account_id = accounts[0].id
                print(f"[step 0] auto-picked first: {unipile_account_id} ({accounts[0].name!r})")
        except Exception as err:
            print(f"[step 0] ERROR listing Unipile accounts: {err}")

    # 1. Upsert operator user
    now = datetime.now(timezone.utc)
    existing = await db.users.find_one({"email": OPERATOR_EMAIL.lower()})
    if existing:
        op_id = existing["_id"]
        print(f"[step 1] operator already exists ({op_id}) — will update in place")
        # also wipe its cofounders so we re-seed cleanly
        n_wiped = (await db.cofounders.delete_many({"operator_id": op_id})).deleted_count
        print(f"[step 1] wiped {n_wiped} existing cofounder(s)")
    else:
        user_doc = {
            "email": OPERATOR_EMAIL.lower(),
            "password_hash": hash_password(OPERATOR_PASSWORD),
            "name": OPERATOR_NAME,
            "timezone": "America/New_York",
            "product_description": None,
            "product_extracted": None,
            "icp_rubric": None,
            "comment_quotas": {
                "A": [35, 40], "B": [22, 25], "C": [14, 16],
                "D": [9, 12], "E": [7, 10], "F": [0, 5],
            },
            "daily_target": 30,
            "hard_floor": 12,
            "run_time_local": "09:00",
            "calendly_webhook_signing_key": secrets.token_urlsafe(32),
            "onboarding_complete": False,
            "paused": False,
            "created_at": now,
            "updated_at": now,
        }
        result = await db.users.insert_one(user_doc)
        op_id = result.inserted_id
        print(f"[step 1] created operator ({op_id})")
        print(f"          login email:    {OPERATOR_EMAIL}")
        print(f"          login password: {OPERATOR_PASSWORD}")

    # 2. Load Taiga configs and build product_extracted
    print("[step 2] Loading Taiga configs and building product_extracted...")
    taiga_rubric, taiga_keywords = load_taiga_configs()
    product_extracted = build_product_extracted(taiga_rubric, taiga_keywords)
    print(f"          target_industries: {product_extracted['target_industries']}")
    print(f"          target_titles ({len(product_extracted['target_titles'])}): "
          f"{product_extracted['target_titles'][:5]}...")
    print(f"          target_geographies: {product_extracted['target_geographies']}")
    print(f"          tier_1 keywords ({len(product_extracted['suggested_keywords']['tier_1'])}): "
          f"{product_extracted['suggested_keywords']['tier_1'][:5]}...")

    # 3. Save product to user doc + override rubric with Taiga rubric verbatim
    # Strip the comment field from the rubric — Pydantic won't accept it.
    taiga_rubric_clean = {k: v for k, v in taiga_rubric.items() if not k.startswith("_")}
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "client_slug": "taiga",
                "product_description": PITCH,
                "product_extracted": product_extracted,
                "icp_rubric": taiga_rubric_clean,
                "comment_quotas": {
                    "A": [35, 40], "B": [22, 25], "C": [14, 16],
                    "D": [9, 12], "E": [7, 10], "F": [0, 5],
                },
                "daily_target": 30,
                "hard_floor": 12,
                "run_time_local": "09:00",
                "slate_recipients": [SLATE_RECIPIENT],
                "calendly_url": None,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    print("[step 3] client_slug=taiga + product + Taiga rubric + quotas + schedule saved")

    # 4. Create 1 cofounder
    cofounder_doc = {
        "operator_id": op_id,
        "display_name": "Nicolas (Taiga rep)",
        "linkedin_url": "https://www.linkedin.com/in/nicolas-vila/",
        "calendly_url": None,
        "email": OPERATOR_EMAIL,
        "daily_volume_target": 12,
        "voice_profile": None,  # drafter will fall back to DEFAULT_VOICE_PROFILE
        "unipile_account_id": unipile_account_id,
        "connect_message_template": None,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }
    cf_result = await db.cofounders.insert_one(cofounder_doc)
    cf_id = cf_result.inserted_id
    print(f"[step 4] created cofounder ({cf_id}) "
          f"unipile_account_id={unipile_account_id!r}")

    # 5. Flip onboarding_complete=True
    await db.users.update_one(
        {"_id": op_id}, {"$set": {"onboarding_complete": True}}
    )
    print("[step 5] onboarding_complete=True")

    line()
    print(f"Operator id: {op_id}")
    print(f"Cofounder id: {cf_id}")
    print(f"Run-now command:")
    print(f"  docker exec infra-worker-1 python -c \\")
    print(f"    \"from app.celery_app import daily_run; "
          f"r = daily_run.delay('{op_id}'); print(r.id)\"")
    line("═")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
