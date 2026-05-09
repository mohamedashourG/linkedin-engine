"""
Bench v2 for the rebuilt drafter, reply drafter, validators, and CR-note
generator.

Run:
    docker exec infra-backend-1 python /app/test_drafter_v2.py
"""
from __future__ import annotations

from pymongo import MongoClient

from app.config import settings
from app.engine.auto_cr import draft_cr_note, extract_topic_phrase
from app.engine.reply_drafter import draft_reply
from app.engine.stages.drafter import draft_comment
from app.engine.stages.validator import (
    validate_cr_note,
    validate_dm,
    validate_public_reply_back,
)


EXCLUDE_AUTHORS = {
    # already-tested authors from prior benches / slates
    "Bartosz Moszczynski",
    "Manoj Narne",
    "Jaswindder Kummar",
    "Partha Sarathi Kundu",
    "Charlie Holland",
    "Mohit Kumar",
    "Aswin Sivaraman R",
    "Vaillant Group Business Services Poland",
}


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


def main() -> None:
    client = MongoClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    # Pull 3 NEW candidates whose authors are NOT in the exclusion set, with
    # substantive post text (>150 chars) and not from a hiring-template post.
    new_candidates = list(
        db.candidates.find(
            {
                "post_text": {"$regex": "^(?!.*(?:#hiring|We are hiring|We're hiring|join my team)).*$", "$options": "i"},
                "author_name": {"$nin": list(EXCLUDE_AUTHORS)},
                "$expr": {"$gt": [{"$strLenCP": {"$ifNull": ["$post_text", ""]}}, 250]},
            },
            {
                "post_text": 1,
                "author_name": 1,
                "author_title": 1,
                "author_company": 1,
                "comment_text": 1,
                "comment_type": 1,
                "source_classification": 1,
                "gate_results": 1,
                "cofounder_id": 1,
            },
        )
        .sort("_id", -1)
        .limit(8)
    )
    # Take first 3 with distinct authors
    seen_authors: set[str] = set()
    picks: list[dict] = []
    for c in new_candidates:
        a = c.get("author_name") or "?"
        if a in seen_authors or a in EXCLUDE_AUTHORS:
            continue
        seen_authors.add(a)
        picks.append(c)
        if len(picks) == 3:
            break

    if len(picks) < 3:
        print(f"Only found {len(picks)} eligible candidates; using what we have.")

    cofounder = db.cofounders.find_one({"_id": picks[0]["cofounder_id"]})
    voice = cofounder.get("voice_profile") or {}
    cofounder_first = (cofounder.get("display_name") or "").strip().split(" ", 1)[0] or "Alex"

    print()
    line("═")
    print("3 NEW posts — drafter v2 with reframe-formula rotation")
    line("═")

    types_to_test = ["E", "B", "A"]  # spread types to exercise different formulas
    formulas_used: list[str] = []

    for i, c in enumerate(picks):
        ctype = types_to_test[i % len(types_to_test)]
        score = int(((c.get("gate_results") or {}).get("icp") or {}).get("total") or 5)
        author = f"{c.get('author_name') or 'Unknown'} ({c.get('author_title') or '?'})"

        print()
        line()
        print(f"## CANDIDATE {i+1}  Type {ctype}  ICP {score}")
        print(f"## Author: {author}")
        line()
        print()
        print("ORIGINAL post:")
        print("  " + (c.get("post_text") or "")[:600])
        print()

        try:
            text, formula_used = draft_comment(
                cofounder_name=cofounder.get("display_name") or "",
                cofounder_tone=voice.get("tone_description") or "",
                cofounder_examples=voice.get("examples") or [],
                post_text=c.get("post_text") or "",
                author_name=c.get("author_name"),
                author_title=c.get("author_title"),
                author_company=c.get("author_company"),
                icp_score=score,
                comment_type=ctype,
                source_classification=c.get("source_classification") or "A",
            )
        except Exception as err:
            print(f"  drafter error: {err}")
            continue

        from app.engine.stages.validator import validate_comment

        result = validate_comment(text, ctype)
        print(f"DRAFTED (formula='{formula_used}'):")
        print("  " + text)
        print()
        print(f"VALIDATOR: ok={result.ok} reason={result.reason}")
        formulas_used.append(formula_used)

    print()
    line("═")
    print(f"DISTINCT REFRAME FORMULAS used: {len(set(formulas_used))} of {len(formulas_used)}")
    for f in set(formulas_used):
        print(f"  - {f}")
    line("═")

    # ---- Reply validator: try to make the LLM emit a banned opener ----
    print()
    line("═")
    print("REPLY DRAFTER + validator — banned-opener attempts")
    line("═")
    sample_post = picks[0].get("post_text") or ""
    sample_our = picks[0].get("comment_text") or "(synthetic comment for bench)"
    # Synthesise a reply that praises the comment to bait an "Exactly" / "Spot on" opener.
    bait_reply = (
        "Spot on. Could not agree more — this is exactly the framing I keep coming back to. "
        "Curious how you would extend it for global launches."
    )
    print()
    line()
    print("Synthetic reply (designed to bait sycophantic opener):")
    print("  " + bait_reply)
    print()
    text, rtype, validation = draft_reply(
        reply_context="PUBLIC_REPLY_BACK",
        cofounder_name=cofounder.get("display_name") or "",
        cofounder_tone=voice.get("tone_description") or "",
        cofounder_first_name=cofounder_first,
        cofounder_calendly_url=cofounder.get("calendly_url") or "https://calendly.com/example/intro",
        author_name=picks[0].get("author_name"),
        author_title=picks[0].get("author_title"),
        author_company=picks[0].get("author_company"),
        original_post=sample_post,
        our_comment=sample_our,
        their_reply=bait_reply,
        they_are_post_author=True,
        prospect_name=picks[0].get("author_name"),
    )
    print(f"PUBLIC_REPLY_BACK output (validator: ok={validation.ok}, reason={validation.reason}):")
    print("  " + text)
    print()
    # Also confirm validator catches a manually-inserted banned opener
    print("Direct validator test on a 'Spot on' opener:")
    test_text = "Spot on. We see this all the time across 30 to 40 mid sized teams, especially when CI ownership is not assigned. Worth defining the boundaries before adding more triggers."
    forced_validation = validate_public_reply_back(test_text)
    print(f"  ok={forced_validation.ok} reason={forced_validation.reason}")

    # ---- DM HOT (lowercase 'lets', calendly own line) ----
    print()
    line("═")
    print("DM HOT — calendly-direct voice")
    line("═")
    text, rtype, validation = draft_reply(
        reply_context="POST_CR_DM_HOT",
        cofounder_name=cofounder.get("display_name") or "",
        cofounder_tone=voice.get("tone_description") or "",
        cofounder_first_name=cofounder_first,
        cofounder_calendly_url=cofounder.get("calendly_url") or "https://calendly.com/mohamed/intro",
        author_name=picks[0].get("author_name"),
        author_title=picks[0].get("author_title"),
        author_company=picks[0].get("author_company"),
        original_post=sample_post,
        our_comment=sample_our,
        their_reply="Happy to chat. Send me a link.",
        they_are_post_author=True,
        prospect_name=picks[0].get("author_name"),
    )
    print(f"validator: ok={validation.ok} reason={validation.reason}")
    print(text)

    # ---- DM WARM (no calendly) ----
    print()
    line("═")
    print("DM WARM — no calendly URL")
    line("═")
    text, rtype, validation = draft_reply(
        reply_context="POST_CR_DM_WARM",
        cofounder_name=cofounder.get("display_name") or "",
        cofounder_tone=voice.get("tone_description") or "",
        cofounder_first_name=cofounder_first,
        cofounder_calendly_url=cofounder.get("calendly_url") or "https://calendly.com/mohamed/intro",
        author_name=picks[0].get("author_name"),
        author_title=picks[0].get("author_title"),
        author_company=picks[0].get("author_company"),
        original_post=sample_post,
        our_comment=sample_our,
        their_reply="(they only liked, no reply)",
        they_are_post_author=False,
        prospect_name=picks[0].get("author_name"),
    )
    print(f"validator: ok={validation.ok} reason={validation.reason}")
    print(text)

    # ---- CR note with explicit signal ----
    print()
    line("═")
    print("CR NOTE — specific_signal='the platform engineer hiring thread on golden paths'")
    line("═")
    note, cr_validation = draft_cr_note(
        cofounder_name=cofounder.get("display_name") or "",
        sender_first=cofounder_first,
        prospect_name="Sarah Chen",
        prospect_title="VP Platform Engineering",
        prospect_company="Stripe",
        trigger_action="Replied 2x on the platform engineer hiring thread",
        specific_signal="the platform engineer hiring thread on golden paths",
    )
    print(f"chars={len(note)}  validator: ok={cr_validation.ok} reason={cr_validation.reason}")
    print(note)

    # ---- CR note with generic signal (must raise) ----
    print()
    line("═")
    print("CR NOTE — generic signal (should raise ValueError)")
    line("═")
    try:
        note, _ = draft_cr_note(
            cofounder_name=cofounder.get("display_name") or "",
            sender_first=cofounder_first,
            prospect_name="Sarah Chen",
            prospect_title=None,
            prospect_company=None,
            trigger_action="(blank)",
            specific_signal="the post we engaged on",  # generic — should reject
        )
        print(f"unexpectedly produced note: {note}")
    except ValueError as err:
        print(f"ValueError raised correctly: {err}")

    # ---- Topic extraction ----
    print()
    line("═")
    print("TOPIC EXTRACTION — gpt-4.1-mini distillation")
    line("═")
    for c in picks:
        phrase = extract_topic_phrase(post_text=c.get("post_text") or "")
        print(f"  '{c.get('author_name')}' → {phrase!r}")
    line("═")


if __name__ == "__main__":
    main()
