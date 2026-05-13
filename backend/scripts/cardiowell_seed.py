"""Cardiowell client seed (test run for nicolasvilaalarcon02@gmail.com).

Targets the nicolasvilaalarcon02@gmail.com operator (formerly the Edge
operator at 6a01bea64d5156521f5122fc) and attaches Nicolas Vila's
LinkedIn-connected Unipile account (_-NlShlUSUqDHiON35OGcQ — verified
status=OK on the Unipile tenant) to its cofounder doc. Comments will
go out under Nicolas Vila's LinkedIn identity. The nicolas@glnkco.com
operator (former Taiga test) is paused.

This seed:
  1. Flips users.client_slug = "cardiowell".
  2. Replaces product_description + product_extracted (titles, industries,
     geographies, pain_points, suggested_keywords) with the maximally-
     thorough Cardiowell ICP from the May 2026 strategy PDF — 11 buyer
     lanes across Tracks A/B/C, all 5 keyword clusters + 35 hashtags.
  3. Replaces users.icp_rubric with the Cardiowell rubric loaded from
     configs/cardiowell/icp_rubric.json.
  4. Sets users.paused = False and onboarding_complete = True.
  5. Transfers Edge cofounder's unipile_account_id onto Nicolas's
     cofounder. Sets display_name = "Yair Lurie" (Cardiowell CEO), updates
     voice_profile from configs/cardiowell/voice_profiles.json.
  6. Sets daily_target + hard_floor + comment_quotas appropriate for a
     "find a lot of good posts" test run.

Run inside the backend container:
    docker exec infra-backend-1 python /app/scripts/cardiowell_seed.py
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings


OPERATOR_EMAIL = "nicolasvilaalarcon02@gmail.com"
OTHER_OPERATOR_EMAIL = "nicolas@glnkco.com"
TARGET_CLIENT_SLUG = "cardiowell"

# Nicolas Vila's personal Unipile account_id on this tenant (status OK,
# verified live via /api/v1/accounts/{id} on 2026-05-13, type=LINKEDIN).
# Tested under Nicolas's own LinkedIn identity — this is the account the
# user explicitly named for the Cardiowell test run.
CARDIOWELL_UNIPILE_ACCOUNT_ID = "_-NlShlUSUqDHiON35OGcQ"


PRODUCT_DESCRIPTION = """Cardiowell is a fully managed remote blood-pressure monitoring platform built around 4G cellular devices (not Bluetooth), purpose-built for primary care, internal medicine, family medicine, cardiology, nephrology, FQHCs, ACOs, MSOs, multi-site medical groups, and health systems running RPM programs.

The structural problem: most hypertensive patients are seen 2-3 times a year. Everything between those visits is invisible. Approximately 120 million Americans have hypertension; only ~24% have BP under control. Uncontrolled hypertension costs the US healthcare system about $131 billion per year. The between-visit monitoring gap is the structural reason BP goes uncontrolled.

The financial wedge: $100-120 per hypertensive patient per month is currently uncaptured CPT revenue via 99457, 99458, 99454, 99453, plus the new 2026 codes 99445 and 99470 that make remote BP reimbursement materially easier to capture. For a 200-patient hypertensive panel, that's ~$20-24K/month in new revenue, recurring.

The technical wedge: most RPM programs die for one of two reasons — Bluetooth devices patients cannot pair, or troubleshooting that lands on staff. 4G cellular devices that transmit automatically eliminate both failure modes. Patients open the box and use the cuff; readings flow to the EHR.

The operations wedge: 30 days to fully running. No upfront cost. 20 minutes per patient per month of clinician time. The practice approves enrollment and does a monthly review; Cardiowell handles devices, shipping, onboarding, monitoring, billing support, and auto-generated documentation.

Buyers are typically: (Track A clinical) practice owners, lead cardiologists, MDs/DOs, nurse practitioners; (Track B operations) CMOs, practice managers, directors of operations, RPM coordinators, VPs of population health, VPs of clinical operations, CMIOs, directors of care management, directors of telehealth, directors of digital health; (Track C finance) CEOs, COOs, CFOs, VPs of revenue cycle, billing directors, medical group administrators. Selling into: independent practices (3-20 providers — sweet spot for Lane 1), multi-site groups, specialty groups (cardiology, nephrology), FQHCs, ACO/MSO clinical-ops leaders, RPM service-company partners, self-insured employer wellness, hospital finance (CFO + RCM offices).

Strategy on comments: lead with the pain, not the product. Demonstrate understanding of the between-visit gap, the uncaptured CPT revenue, or the Bluetooth failure mode before introducing Cardiowell. Cardiowell can be named when the thread explicitly invites it (Track A "go-direct" angle on advice-seeking posts, or RFP-style asks). Otherwise stay topical and credible.
"""


# ─── product_extracted (target_titles / industries / geographies / pain_points / suggested_keywords) ───
PRODUCT_EXTRACTED = {
    "target_titles": [
        # ── Track A clinical
        "Chief Executive Officer",
        "CEO",
        "Practice Owner",
        "Founding Physician",
        "Lead Cardiologist",
        "Cardiologist",
        "Preventive Cardiologist",
        "Nephrologist",
        "Practice Lead",
        "Medical Director",
        "MD",
        "DO",
        "Internal Medicine physician",
        "Family Medicine physician",
        "Primary Care physician",
        "Nurse Practitioner",
        "NP",
        "Physician Assistant",
        "PA-C",
        "Registered Nurse",
        "RN",
        # ── Track B operations
        "Chief Operating Officer",
        "COO",
        "Chief Medical Officer",
        "CMO",
        "Chief Medical Information Officer",
        "CMIO",
        "Chief Nursing Officer",
        "CNO",
        "Practice Manager",
        "Office Administrator",
        "Office Manager",
        "Director of Operations",
        "Director of Clinical Operations",
        "Director of Practice Operations",
        "Director of Care Management",
        "Director of Population Health",
        "Director of Quality",
        "Director of Remote Patient Monitoring",
        "Director of Telehealth",
        "Director of Digital Health",
        "VP of Population Health",
        "VP of Clinical Operations",
        "VP of Digital Health",
        "VP of Telehealth",
        "VP of Quality",
        "Chief Quality Officer",
        "RPM Coordinator",
        "Population Health Coordinator",
        "Care Manager",
        "Nurse Manager",
        # ── Track C finance / admin
        "Chief Financial Officer",
        "CFO",
        "Chief Administrative Officer",
        "Medical Group Administrator",
        "Practice Administrator",
        "VP of Revenue Cycle",
        "VP of Finance",
        "Director of Revenue Cycle Management",
        "Director RCM",
        "Billing Director",
        # ── Lane 9 RPM partner channel + Lane 10 employer wellness
        "VP of Partnerships",
        "VP of Product",
        "Founder",
        "Co-Founder",
        "President",
        "VP of Benefits",
        "Director of Employee Wellness",
        "Director of Benefits",
        "Chief People Officer",
    ],
    "target_industries": [
        # tier_1
        "Hospitals and Health Care",
        "Hospital and Health Care",
        "Health Care Services",
        "Medical Practice",
        "Medical Groups",
        "Medical Group Practice",
        "Health Systems",
        "Hospital Network",
        "Federally Qualified Health Centers",
        "FQHC",
        "Community Health Centers",
        "Cardiology",
        "Nephrology",
        "Internal Medicine",
        "Family Medicine",
        "Primary Care",
        "Multi-Specialty Group",
        # tier_2
        "Accountable Care Organization",
        "ACO",
        "Management Services Organization",
        "MSO",
        "Clinically Integrated Network",
        "Population Health Management",
        "Telehealth",
        "Telemedicine",
        "Remote Patient Monitoring",
        "Digital Health",
        "Healthcare Technology",
        "Health Insurance",
        # tier_3
        "Employee Benefits",
        "Wellness and Fitness Services",
        "Health Wellness and Fitness",
        "Insurance",
    ],
    "target_geographies": [
        "United States",
        "US",
        "USA",
        "California",
        "Florida",
        "Arizona",
        "Colorado",
        "Texas",
        "New York",
        "Georgia",
    ],
    "target_pain_points": [
        "Hypertensive patients only seen 2-3 times a year, between-visit BP is invisible",
        "BP goes uncontrolled between visits despite medication and follow-up",
        "Roughly 120M Americans hypertensive, only ~24% controlled, $131B annual healthcare cost",
        "Bluetooth RPM devices fail because patients cannot pair them",
        "Patient-side app troubleshooting lands on practice staff and burns clinician time",
        "RPM programs stall on enrollment, device shipping, and onboarding logistics",
        "$100-120 per hypertensive patient per month in CPT 99457/99458 reimbursement going uncaptured",
        "New 2026 CPT codes 99445 and 99470 not being captured by RCM teams",
        "RPM compliance threshold (16 days of readings) not being hit",
        "MSSP, ACO REACH, and VBC quality scores tied to BP control measures",
        "FQHCs missing UDS BP control quality metrics",
        "Practice has no infrastructure to monitor chronic patients between visits",
        "Hiring an RPM coordinator is operationally expensive for small/mid practices",
        "Documentation burden for RPM billing creates audit risk",
        "Specialty groups (cardiology, nephrology) want chronic-care infra without internal build",
        "Health system population health teams missing claims-data leading indicators for BP",
        "Self-insured employers carrying cardiovascular disease risk in claims with no intervention layer",
        "Hospital CFOs missing recurring revenue lines tied to chronic disease panels",
        "Health-system margin pressure from shifting payer mix",
        "Physicians leaving hospital medicine to launch telehealth practices have no plug-in chronic care stack",
    ],
    "suggested_keywords": {
        "tier_1": [
            "uncontrolled hypertension",
            "hypertension management",
            "hypertension control",
            "blood pressure management",
            "blood pressure monitoring",
            "remote patient monitoring",
            "RPM program",
            "remote BP monitoring",
            "between visit monitoring",
            "between visit monitoring gap",
            "CPT 99457",
            "CPT 99458",
            "CPT 99454",
            "CPT 99453",
            "CPT 99445",
            "CPT 99470",
            "remote monitoring reimbursement",
            "uncaptured CPT revenue",
            "Bluetooth device patient",
            "RPM program failing",
            "cellular RPM device",
            "4G cellular monitoring",
            "patient compliance hypertension",
            "medication adherence hypertension",
            "telehealth practice launch",
            "physician leaving hospital medicine",
            "starting independent practice",
            "SMBP",
            "home blood pressure monitoring",
        ],
        "tier_2": [
            "chronic disease management",
            "chronic care management",
            "CCM program",
            "cardiovascular risk",
            "preventive cardiology",
            "value-based care quality bonus",
            "value-based contract performance",
            "MSSP quality measure",
            "MSSP ACO",
            "Medicare Shared Savings Program",
            "shared savings ACO",
            "ACO REACH",
            "downside risk contract",
            "BP control quality measure",
            "Medicare billing CPT",
            "CPT code update 2026",
            "2026 CPT changes",
            "revenue cycle management healthcare",
            "denied claims primary care",
            "prior authorization burden",
            "RPM coordinator role",
            "population health coordinator",
            "care manager role",
            "care management workflow",
            "FQHC quality reporting",
            "UDS measures FQHC",
            "HRSA quality metrics",
            "margin pressure health system",
            "practice operations strain",
            "MIPS reporting",
        ],
        "tier_3": [
            "AI in healthcare",
            "healthcare AI scribe",
            "ambient documentation",
            "ambient AI scribe",
            "AI scribe primary care",
            "telehealth practice",
            "virtual care primary care",
            "virtual first practice",
            "Epic EHR optimization",
            "athenahealth practice",
            "athenahealth RPM",
            "Cerner workflow",
            "eClinicalWorks",
            "digital health adoption",
            "patient monitoring platform",
            "cellular device healthcare",
            "connected health platform",
            "population health technology",
            "population health platform",
            "physician burnout documentation",
            "new CMO appointment",
            "new CMIO appointment",
            "RPM vendor selection",
            "evaluating RPM vendors",
            "hypertension",
            "blood pressure",
            "RPM",
            "primary care",
            "FQHC",
            "ACO",
        ],
    },
}


# Resolved schedule + quotas (operator-level)
DAILY_TARGET = 60
HARD_FLOOR = 20
RUN_TIME_LOCAL = "09:00"

COMMENT_QUOTAS = {
    "A": [40, 50],  # high-fit, well-anchored
    "B": [25, 30],
    "C": [15, 20],
    "D": [8, 12],
    "E": [3, 6],
    "F": [0, 2],
}


def _configs_dir() -> Path:
    """Returns /app/configs/cardiowell when running inside the container."""
    # backend/scripts/cardiowell_seed.py → /app/scripts/cardiowell_seed.py in container
    # configs live at /app/configs in our compose mount
    here = Path(__file__).resolve()
    # Container mounts the repo's configs/ at /configs (read-only).
    for candidate in (
        Path("/configs/cardiowell"),
        Path("/app/configs/cardiowell"),
        here.parent.parent.parent / "configs" / "cardiowell",
        here.parent.parent / "configs" / "cardiowell",
    ):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Cannot find configs/cardiowell — tried /configs/cardiowell, /app/configs/cardiowell, repo-relative."
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    line("═")
    print(f"Cardiowell seed → operator={OPERATOR_EMAIL}")
    line("═")

    cfg_dir = _configs_dir()
    print(f"[init] configs dir = {cfg_dir}")

    # Load Cardiowell rubric from the on-disk config so we keep one source of truth.
    icp_rubric = _load_json(cfg_dir / "icp_rubric.json")
    print(
        f"[init] icp_rubric loaded — threshold={icp_rubric.get('threshold')}, "
        f"axes=[title, industry, geography, stage]"
    )

    # ── Step 1: find target operator ────────────────────────────────────────
    op = await db.users.find_one({"email": OPERATOR_EMAIL})
    if not op:
        print(f"FATAL: operator {OPERATOR_EMAIL} not found"); return
    op_id = op["_id"]
    print(f"[step 1] operator_id={op_id} (was client_slug={op.get('client_slug')!r}, paused={op.get('paused')})")

    # ── Step 2: pin Nicolas Vila's Unipile account ──────────────────────────
    other_op = await db.users.find_one({"email": OTHER_OPERATOR_EMAIL})
    cardiowell_unipile = CARDIOWELL_UNIPILE_ACCOUNT_ID
    print(f"[step 2] cofounder unipile_account_id = {cardiowell_unipile!r} (Nicolas Vila, verified OK)")

    # ── Step 3: rewrite operator doc ────────────────────────────────────────
    now = datetime.now(timezone.utc)
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "client_slug": TARGET_CLIENT_SLUG,
                "product_description": PRODUCT_DESCRIPTION,
                "product_extracted": PRODUCT_EXTRACTED,
                "icp_rubric": icp_rubric,
                "paused": False,
                "onboarding_complete": True,
                "run_time_local": RUN_TIME_LOCAL,
                "daily_target": DAILY_TARGET,
                "hard_floor": HARD_FLOOR,
                "comment_quotas": COMMENT_QUOTAS,
                "updated_at": now,
            }
        },
    )
    print(
        f"[step 3] users.{op_id}: client_slug=cardiowell, paused=False, "
        f"product_extracted={{titles={len(PRODUCT_EXTRACTED['target_titles'])}, "
        f"industries={len(PRODUCT_EXTRACTED['target_industries'])}, "
        f"geos={len(PRODUCT_EXTRACTED['target_geographies'])}, "
        f"pain={len(PRODUCT_EXTRACTED['target_pain_points'])}, "
        f"kw_t1={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_1'])}, "
        f"kw_t2={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_2'])}, "
        f"kw_t3={len(PRODUCT_EXTRACTED['suggested_keywords']['tier_3'])}}}"
    )

    # ── Step 4: pause the OTHER operator (nicolas@glnkco.com Taiga) ────────
    if other_op:
        await db.users.update_one(
            {"_id": other_op["_id"]}, {"$set": {"paused": True, "updated_at": now}}
        )
        print(f"[step 4] {OTHER_OPERATOR_EMAIL} operator paused")

    # ── Step 5: transfer Unipile to Nicolas's cofounder + rebrand ──────────
    cf = await db.cofounders.find_one({"operator_id": op_id})
    if not cf:
        print("FATAL: Nicolas cofounder not found"); return

    voice_profiles = _load_json(cfg_dir / "voice_profiles.json")
    cardiowell_voice = {
        "tone_description": (
            "Cardiowell rep voice. Lead with the pain, not the product. Demonstrate "
            "understanding of the between-visit monitoring gap, uncaptured CPT revenue, "
            "or Bluetooth failure modes before introducing Cardiowell. Use specific, "
            "credible numbers ($100-120/patient/month via CPT 99457/99458, new 2026 codes "
            "99445/99470, 120M hypertensive Americans, ~24% controlled, $131B annual "
            "cost, 30-day go-live, 20 min/patient/month of clinical time). Plain sentences. "
            "No bullets. No em-dashes. End with a question. Cardiowell is named only when "
            "the thread invites it (Angle 3 / explicit advice-seeking)."
        ),
        "shared_voice_notes": voice_profiles.get("shared_voice_notes") or [],
        "preferred_close_patterns": voice_profiles.get("preferred_close_patterns") or [],
        "avoided_phrases": voice_profiles.get("avoided_phrases") or [],
        "source_a_template": "",
        "source_b_template": "",
        "examples": [],
    }

    cf_updates: dict[str, Any] = {
        "display_name": "Nicolas Vila (Cardiowell rep)",
        "voice_profile": cardiowell_voice,
        "unipile_account_id": cardiowell_unipile,
        "active": True,
        "updated_at": now,
    }

    await db.cofounders.update_one({"_id": cf["_id"]}, {"$set": cf_updates})
    print(
        f"[step 5] cofounder {cf['_id']}: display_name=Nicolas Vila (Cardiowell rep), "
        f"unipile_account_id={cardiowell_unipile!r}, voice_profile=cardiowell"
    )

    # ── Step 6: take Unipile off the OTHER operator's cofounder ────────────
    if other_op:
        other_cf = await db.cofounders.find_one(
            {"operator_id": other_op["_id"], "unipile_account_id": {"$nin": [None, ""]}}
        )
        if other_cf:
            await db.cofounders.update_one(
                {"_id": other_cf["_id"]},
                {"$set": {"unipile_account_id": None, "active": False, "updated_at": now}},
            )
            print(f"[step 6] {OTHER_OPERATOR_EMAIL} cofounder {other_cf['_id']}: unipile cleared, active=False")

    # ── Final summary ───────────────────────────────────────────────────────
    line()
    op_after = await db.users.find_one({"_id": op_id})
    cf_after = await db.cofounders.find_one({"_id": cf["_id"]})
    print(f"Cardiowell operator: slug={op_after.get('client_slug')!r} paused={op_after.get('paused')}")
    print(f"Cofounder: name={cf_after.get('display_name')!r} unipile={cf_after.get('unipile_account_id')!r} active={cf_after.get('active')}")
    print(f"Operator id (for run-now): {op_id}")
    print(f"Cofounder id: {cf['_id']}")
    line("═")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
