"""GLNK reset + reseed for mohamed@example.com.

Steps:
  0. Preserve any existing Unipile account_id on Mohamed's cofounder record.
  1. Wipe cofounders for this operator + flip onboarding_complete=False.
  2. Run the real ICP extractor on the GLNK pitch (Azure call).
  3. Save product_description + product_extracted + icp_rubric.
  4. For each of 4 cofounders: insert doc + run real voice-template builder
     (Azure call) to populate source_a_template + source_b_template.
  5. Re-attach the preserved Unipile account_id to Mohamed Mahdi (cofounder #4),
     since that's the only LinkedIn account connected on the tenant.
  6. Set Calendly + schedule + comment_quotas + spec ICP rubric override
     + slate_recipients.
  7. Flip onboarding_complete=True.

Run inside the backend container:
    docker exec infra-backend-1 python /app/scripts/glnk_seed.py
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.services.icp_extractor import extract as extract_icp
from app.services.voice_profile import VoiceExample, build_templates


OPERATOR_EMAIL = "mohamed@example.com"
RECIPIENT_EXTRA = "mohamaachour@gmail.com"


PITCH = """G LNK is a prescriber-data and commercial-intelligence product for pharma and specialty biotech. We map prescriber patterns on top of claims data, with depth on cash-pay DTC dynamics, specialty launch readiness, and upstream-referrer identification.

Customers use G LNK to: tighten the HCP universe before reps onboard so the launch starts with the right call list (not the right call list after Q1 data comes in); map the cash-pay prescriber base for specialty and DTC drugs (especially GLP-1, telehealth-driven categories, and cash-pay weight management / men's health / women's health); identify upstream referrers in specialty disease launches like cell therapy, rare disease, and oncology, the neurologists, PCPs, and specialists who recognize the resistance signal versus those who titrate another year; track primary-care prescribing shifts in categories that used to live in specialty (GLP-1 PCP migration is the canonical example); quantify the diagnostic ecosystem for rare disease and specialty launches before commercial sequencing locks in; and help medical affairs / MSL functions tie scientific insight to actual brand decisions, not just to slide decks.

Buyers are typically: founders, CCO/VP Commercial, VP Sales, VP Marketing, Head of Medical Affairs, MSL Directors, and increasingly healthtech VCs / growth equity investors who want prescriber-pattern overlays on their investment theses. ACVs run from $50K (single-vertical map) to $250K+ (ongoing data + analyst access).

The wedge for most conversations: prescriber-pattern data is the layer that decides whether a launch starts at scale or scales after a year of cleanup, and most pharma commercial teams still treat it as a narrative input rather than infrastructure.
"""


def _ex(post_topic: str, post_excerpt: str, comment_text: str) -> dict[str, str]:
    """Voice example payload — combines topic + excerpt as the `post` field
    so the LLM has context when building source_a / source_b templates."""
    return {
        "post": f"[Topic: {post_topic}]\n{post_excerpt}",
        "comment": comment_text,
    }


COFOUNDERS: list[dict[str, Any]] = [
    {
        "display_name": "Alex Gregoriades",
        "linkedin_url": "https://www.linkedin.com/in/alexandergregoriades/",
        "calendly_url": "https://calendly.com/alexander-glnkco/meeting-with-alex",
        "email": "alex@glnkco.com",
        "daily_volume_target": 12,
        "tone": (
            "Direct, declarative, lowercase-casual in DMs. Specific numbers and "
            "named cohorts in every comment ('250 epileptologists', '47%', '500 "
            "physicians', '20% of the call list'). Reframe-first openers, never "
            "agree-and-extend, never 'great point', never 'love this'. Soft "
            "product mention woven naturally ('we have been mapping that at G "
            "LNK'), never a pitch. Conversational, not corporate. No buzzwords "
            "(no leverage / synergy / value-add / best-in-class / game-changer). "
            "Lowercase 'lets' instead of 'let's' in DMs. Sign DMs with first "
            "name only ('Alex')."
        ),
        "examples": [
            _ex(
                "UCB / Neurona Therapeutics cell therapy acquisition",
                "UCB acquired Neurona Therapeutics for NRTX-1001, cell therapy for drug-resistant temporal lobe epilepsy ($650M + $500M milestones).",
                "Cell therapy launches like this look like awareness plays from the outside, but operationally they are pure targeting problems. The refractory TLE cohort sits with maybe 250 epileptologists nationally, and most of them already triage the same way. The commercial question is not who tells them about NRTX-1001, it is which neurologists upstream are actually identifying drug-resistant cases versus titrating the third AED for another year. UCB will get good ROI not from broad reach but from finding the upstream referrers who currently miss the resistance signal.",
            ),
            _ex(
                "Amazon One Medical GLP-1 program",
                "Amazon One Medical adding GLP-1 to primary care delivery channel.",
                "The Amazon program is the cleanest live test of cash-pay DTC pharma at scale because it sits inside an existing primary care channel rather than a standalone telehealth flow. The signal worth watching is which PCPs in the One Medical network actually start writing GLP-1s versus referring out, since that adoption curve is what every cash-pay manufacturer needs to model. We have been mapping that prescriber-level shift at G LNK for a few cash-pay teams and the dispersion is bigger than people expect. Would be interesting to compare your forecast view against the actual writer pattern over the next two quarters.",
            ),
            _ex(
                "Patient access / launch friction (Walter Toro)",
                "47% of patients face access barriers in launch year",
                "The 47% number is the part that should scare every launch finance team, because forecasts built on coverage-status assumptions are sitting on a cliff. Access quality measurement has to graduate from formulary tier to time-to-first-fill at the HCP level. The real question is whether the field team knows which 500 physicians account for most of the friction, because that is where launch velocity is actually decided.",
            ),
            _ex(
                "Virtual rheumatology / capacity gap (Blake Siewert)",
                "Rheumatology access bottleneck in adult care",
                "The rheum capacity gap is one of the most quietly painful access problems in adult care, ortho referrals make it worse not better. We pulled the rheumatology prescriber and referrer base recently across a few mid sized health systems, happy to share what we saw if useful. DM me if a quick swap of notes helps the model conversations.",
            ),
            _ex(
                "MSL career reflection (Shahad Alotaibi personal-narrative)",
                "MSL role science meets strategy career reflection",
                "MSL strategy lives or dies on whether the scientific insight ever changes a brand decision. Does it?",
            ),
        ],
    },
    {
        "display_name": "Rayan Ghandour",
        "linkedin_url": "https://www.linkedin.com/in/rayan-ghandour/",
        "calendly_url": "https://calendly.com/rayan-glnkco/call-with-rayan-intro-g-lnk",
        "email": "rayan@glnkco.com",
        "daily_volume_target": 7,
        "tone": (
            "Direct, slightly warmer than Alex. Same structural rules: reframe "
            "openers, specific numbers, soft G LNK mentions only. Strong on "
            "rare disease and patient-identification framing, plus genetic "
            "testing partnerships and biotech launch sequencing. Lowercase "
            "'lets' in DMs. Sign DMs with first name only ('Rayan')."
        ),
        "examples": [
            _ex(
                "Rare disease commercial / oncology vs rare scaling",
                "Dr Erum Banday MD on rare disease commercial structure, MSL ecosystem questions",
                "The diagnostic ecosystem framing is the one most rare disease teams skip in their first launch. Time to suspicion is the right KPI, the patient identification work has to live in EMR and lab partnerships, not in territory plans. The MSL ecosystem build vs prescriber hunt distinction is the part most field organizations underweight by 12 months. Would love to compare notes on how you are sequencing genetic testing partnerships, here is my calendar: https://calendly.com/rayan-glnkco/call-with-rayan-intro-g-lnk",
            ),
            _ex(
                "Biotech launch sequencing (Diana Ji)",
                "Simultaneous launch problem in biotech, companion diagnostics timing",
                "Yes exactly that, the simultaneous launch problem is what most ops leaders underprice. Companion diagnostics is the cleanest example, lab access has to be wired three months before drug launch or the prescriber writes blind. Field execution is the one most teams cant catch up on once they fall behind. Would love to compare notes offline if useful.",
            ),
            _ex(
                "Specialist-led to PCP-led prescriber shift (Rohini Khanna Pangasa)",
                "PCP-led execution requires prescriber identification at granularity specialty teams never had",
                "The structural shift framing is the one most launch teams miss. PCP-led execution requires prescriber identification at a granularity specialty teams never had to build. The targeting precision plus EMR aware sequencing piece is where most legacy stacks fail. Curious whether you are seeing the medical affairs side keeping pace with field commercial on the workflow.",
            ),
        ],
    },
    {
        "display_name": "Michael Colivet",
        "linkedin_url": "https://www.linkedin.com/in/michael-colivet/",
        "calendly_url": "https://calendly.com/michael-glnkco/1-1-michael-colivet",
        "email": "michael@glnkco.com",
        "daily_volume_target": 7,
        "tone": (
            "Direct, sharper edge on commercial-readiness and pre-commercial "
            "pharma topics. Same structural rules. Strong on Type-B and Type-F "
            "warm-light voices for Sales Nav harvested ICP-adjacent profiles. "
            "Frequently engages on AI biotech execution, brand inflection "
            "points, and global pharma M&A. Lowercase 'lets' in DMs. Sign DMs "
            "with first name only ('Michael')."
        ),
        "examples": [
            _ex(
                "Recursion AI biotech execution (Suzanne Morgan)",
                "AZ Executive Director on AI biotech execution, ecosystem mapping",
                "The ecosystem mapping point is the one most pre-commercial teams skip until 18 months in. AI biotechs that deliver drugs win on the prescriber identification work that started during pivotal, not on the launch advertising. Time to peak script volume is decided by how dense your prescriber map is six months before the first DEA number is written.",
            ),
            _ex(
                "Ozempic brand / Wegovy MASH India SEC approval (Aditi Sinha)",
                "Brand-as-common-noun moment for Novo, indication expansion",
                "The brand-as-common-noun moment is exactly the commercial inflection Novo has to manage carefully. Wegovy SEC approval for MASH in India is the kind of indication expansion that needs HCP targeting precision the legacy stack was never built for. The brand equity piece is gold but it can also collapse the line distinction commercially.",
            ),
            _ex(
                "Indian pharma M&A (Dibya Singh Hota)",
                "Sun Torrent Dr Reddys consolidation pattern in global pharma M&A cycle",
                "Thanks for the read. The Sun Torrent Dr Reddys consolidation pattern is the part of the global pharma M&A cycle that Western trade press underreports. Curious how the field force integration plays out, the commercial muscle in inorganic growth is where most deals leak value.",
            ),
        ],
    },
    {
        "display_name": "Mohamed Mahdi",
        "linkedin_url": "https://www.linkedin.com/in/mohamed-mahdi/",
        "calendly_url": "https://calendly.com/alexander-glnkco/meeting-with-alex",
        "email": "mohamaachour@gmail.com",
        "daily_volume_target": 4,
        "tone": (
            "Direct, lowercase casual, technical, MENA-flavored when relevant. "
            "Same structural rules as Alex: reframe openers, specific numbers, "
            "soft G LNK mentions only, lowercase 'lets' in DMs. Mohamed sits "
            "between the engineering and commercial sides of G LNK and tends "
            "to draft on technical-rigor and data-quality angles where Alex / "
            "Rayan would lean commercial-strategy. Sign DMs with first name "
            "only ('Mohamed')."
        ),
        "examples": [
            _ex(
                "Prescriber data quality / identity resolution (PLACEHOLDER)",
                "Placeholder example — replace with real Mohamed comment via /onboarding/voice/<id>",
                "Prescriber data quality is the right wedge, but the underrated layer is identity resolution across NPI, claims, and email-domain matched events. Most platforms stop at NPI level and miss the same physician writing under three different employer affiliations. The teams that close that gap end up with a measurably tighter HCP target list before the field even ramps.",
            ),
            _ex(
                "Cash-pay attribution in DTC pharma (PLACEHOLDER)",
                "Placeholder",
                "Cash-pay attribution is one of the quiet failure modes nobody screens for early. The PCP migration on GLP-1 alone shifted the prescriber base by 30 to 40 percent in 6 months, and most commercial dashboards are still built on the specialty-driven version. The fix is upstream, clean event-level data tied to cash-pay flows, not retrofitted segments.",
            ),
            _ex(
                "MENA pharma commercial (PLACEHOLDER)",
                "Placeholder",
                "GCC pharma commercial is structurally different from US in one part most teams miss, the prescriber concentration is much higher and KOL targeting has more leverage. We have been mapping that pattern at G LNK for clients running launches across UAE and KSA, and the precision delta against Western-style HCP segmentation is bigger than expected.",
            ),
        ],
    },
]


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    op = await db.users.find_one({"email": OPERATOR_EMAIL})
    if not op:
        print(f"FATAL: operator {OPERATOR_EMAIL} not found"); return
    op_id = op["_id"]

    line("═")
    print(f"GLNK reset + seed  operator={op_id}  email={OPERATOR_EMAIL}")
    line("═")

    # ── Step 0: preserve unipile_account_id from any existing cofounder ─────
    existing = await db.cofounders.find_one(
        {"operator_id": op_id, "unipile_account_id": {"$nin": [None, ""]}}
    )
    preserved_unipile = existing.get("unipile_account_id") if existing else None
    print(f"[step 0] preserved unipile_account_id = {preserved_unipile!r}")

    # Wipe cofounders + flip onboarding flag.
    n_wiped = (await db.cofounders.delete_many({"operator_id": op_id})).deleted_count
    await db.users.update_one(
        {"_id": op_id}, {"$set": {"onboarding_complete": False}}
    )
    print(f"[step 0] wiped {n_wiped} cofounder(s) + flipped onboarding_complete=False")

    # ── Step 1: real ICP extract via Azure ──────────────────────────────────
    print("[step 1] Azure: extracting ICP from product description...")
    extracted = await extract_icp(PITCH)
    print(
        f"[step 1] tier_1 keywords ({len(extracted['product_extracted']['suggested_keywords']['tier_1'])}): "
        f"{extracted['product_extracted']['suggested_keywords']['tier_1'][:5]}..."
    )

    # Save product to user doc.
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "product_description": PITCH,
                "product_extracted": extracted["product_extracted"],
                "icp_rubric": extracted["icp_rubric"],
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    print("[step 1] saved product_description + product_extracted + icp_rubric")

    # ── Step 2: 4 cofounders + voice templates ──────────────────────────────
    inserted_ids: dict[str, Any] = {}
    for i, cf in enumerate(COFOUNDERS, start=1):
        now = datetime.now(timezone.utc)
        cf_doc = {
            "operator_id": op_id,
            "display_name": cf["display_name"],
            "linkedin_url": cf["linkedin_url"],
            "calendly_url": cf["calendly_url"],
            "email": cf["email"],
            "daily_volume_target": cf["daily_volume_target"],
            "voice_profile": None,
            "unipile_account_id": None,
            "connect_message_template": None,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        result = await db.cofounders.insert_one(cf_doc)
        cf_id = result.inserted_id
        inserted_ids[cf["display_name"]] = cf_id
        print(f"[step 2.{i}] created cofounder {cf['display_name']!r} ({cf_id})")

        print(f"[step 2.{i}] Azure: building voice templates for {cf['display_name']}...")
        try:
            templates = await build_templates(
                tone_description=cf["tone"],
                examples=[VoiceExample(**ex) for ex in cf["examples"]],
            )
        except Exception as err:
            print(f"[step 2.{i}] WARN voice template build failed: {err}")
            templates = {"source_a_template": "", "source_b_template": ""}
        await db.cofounders.update_one(
            {"_id": cf_id},
            {
                "$set": {
                    "voice_profile": {
                        "tone_description": cf["tone"],
                        "examples": cf["examples"],
                        "source_a_template": templates["source_a_template"],
                        "source_b_template": templates["source_b_template"],
                    },
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        print(f"[step 2.{i}] voice_profile saved")

    # Re-attach Unipile to Mohamed Mahdi (the only LinkedIn we actually have
    # connected on this Unipile tenant).
    if preserved_unipile and "Mohamed Mahdi" in inserted_ids:
        await db.cofounders.update_one(
            {"_id": inserted_ids["Mohamed Mahdi"]},
            {"$set": {"unipile_account_id": preserved_unipile}},
        )
        print(
            f"[step 2.5] re-attached Unipile account_id to Mohamed Mahdi: "
            f"{preserved_unipile}"
        )

    # ── Step 3: Calendly + signing key ──────────────────────────────────────
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "calendly_url": "https://calendly.com/alexander-glnkco/meeting-with-alex",
                "calendly_webhook_signing_key": secrets.token_urlsafe(32),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    print("[step 3] calendly + signing key saved")

    # ── Step 4: schedule ────────────────────────────────────────────────────
    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "run_time_local": "09:00",
                "daily_target": 30,
                "hard_floor": 12,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    print("[step 4] schedule saved (run_time_local=09:00, daily_target=30, hard_floor=12)")

    # ── Step 5/6: spec ICP rubric override + comment_quotas + slate_recipients ──
    extracted_titles = extracted["product_extracted"].get("target_titles") or []
    if isinstance(extracted_titles, dict):
        title_t1 = extracted_titles.get("tier_1") or []
        title_t2 = extracted_titles.get("tier_2") or []
        title_t3 = extracted_titles.get("tier_3") or []
    else:
        # extractor flattens titles to a list — split heuristically into thirds
        n = len(extracted_titles)
        title_t1 = extracted_titles[: max(1, n // 3)]
        title_t2 = extracted_titles[max(1, n // 3) : max(2, 2 * n // 3)]
        title_t3 = extracted_titles[max(2, 2 * n // 3) :]

    # Augment with the spec's hard-required titles so they're definitely in.
    spec_t1 = ["Founder", "CEO", "Co-Founder", "Chief Commercial Officer", "CCO",
               "VP Commercial", "VP Sales", "VP Marketing", "Head of Commercial",
               "Head of Sales", "Head of Marketing", "Chief Medical Officer", "CMO"]
    title_t1 = list(dict.fromkeys([*spec_t1, *title_t1]))

    spec_rubric = {
        "title": {
            "tiers": [
                {"matches": title_t1, "score": 4},
                {"matches": title_t2, "score": 3},
                {"matches": title_t3, "score": 2},
            ]
        },
        "industry": {
            "tiers": [
                {"matches": ["Pharma", "Biotech", "Specialty pharma", "Cell therapy", "Cash-pay DTC pharma"], "score": 3},
                {"matches": ["MedTech", "Healthtech", "Medical affairs / MSL"], "score": 2},
                {"matches": ["Healthcare investing (VC + growth equity)"], "score": 2},
            ]
        },
        "geography": {
            "tiers": [
                {"matches": ["United States"], "score": 2},
                {"matches": ["United Kingdom", "European Union"], "score": 1},
                {"matches": ["GCC"], "score": 1},
            ]
        },
        "stage": {
            "tiers": [
                {"matches": ["pre-launch", "pre-IPO", "growth", "post-Series-A"], "score": 1}
            ]
        },
        "threshold": 6,
    }

    op2 = await db.users.find_one({"_id": op_id})
    recipients = list(op2.get("slate_recipients") or [])
    if RECIPIENT_EXTRA not in recipients:
        recipients.append(RECIPIENT_EXTRA)

    await db.users.update_one(
        {"_id": op_id},
        {
            "$set": {
                "comment_quotas": {
                    "A": [35, 40], "B": [22, 25], "C": [14, 16],
                    "D": [9, 12], "E": [7, 10], "F": [0, 5],
                },
                "icp_rubric": spec_rubric,
                "slate_recipients": recipients,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    print("[step 5/6] spec icp_rubric + comment_quotas + slate_recipients saved")

    # ── Step 7: flip onboarding_complete=True ───────────────────────────────
    await db.users.update_one(
        {"_id": op_id}, {"$set": {"onboarding_complete": True}}
    )

    # ── Final summary ───────────────────────────────────────────────────────
    line()
    n_cf = await db.cofounders.count_documents({"operator_id": op_id})
    n_active = await db.cofounders.count_documents({"operator_id": op_id, "active": True})
    n_unipile = await db.cofounders.count_documents(
        {"operator_id": op_id, "unipile_account_id": {"$nin": [None, ""]}}
    )
    print(f"Cofounder count: {n_cf}")
    print(f"Active cofounders: {n_active}")
    print(f"Unipile-connected cofounders: {n_unipile}")
    print(f"Slate recipients: {recipients}")
    print(f"Operator id (for run-now): {op_id}")
    line("═")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
