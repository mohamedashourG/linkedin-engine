"""
Bench the rebuilt drafter + validator + reply_drafter + auto_cr CR-note
generator against:
  1. Known real LinkedIn posts (3 from the most recent sealed slate)
  2. A synthetic reply for the public reply-back, hot-DM, warm-DM contexts
  3. A synthetic CR-note request

Prints results to stdout. Run inside the backend container:
    docker exec infra-backend-1 python /app/test_drafter_rebuild.py
"""
from __future__ import annotations

from pymongo import MongoClient

from app.config import settings
from app.engine.auto_cr import draft_cr_note
from app.engine.reply_drafter import draft_reply
from app.engine.stages.drafter import draft_comment
from app.engine.stages.validator import validate_comment, validate_cr_note


def line(c: str = "─", n: int = 78) -> None:
    print(c * n)


def main() -> None:
    client = MongoClient(settings.mongodb_uri)
    db = client[settings.mongodb_db]

    # Pull the most recent SEALED slate's slated candidates.
    slate = db.slate_runs.find_one({"status": "sealed"}, sort=[("created_at", -1)])
    if not slate:
        print("No sealed slate yet — run /api/slate/run-now first.")
        return
    cands = list(
        db.candidates.find(
            {"slate_run_id": slate["_id"]},
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
        ).limit(3)
    )
    if not cands:
        print("Sealed slate has no candidates.")
        return

    cofounder = db.cofounders.find_one({"_id": cands[0]["cofounder_id"]})
    if not cofounder:
        print("Cofounder missing for slate.")
        return
    voice = cofounder.get("voice_profile") or {}

    print()
    line("═")
    print("REBUILT DRAFTER vs ORIGINAL — same 3 posts, new prompt")
    line("═")

    # Force one of each high-leverage type so we exercise A, C, E.
    types_to_test = ["E", "A", "C"]

    for i, c in enumerate(cands):
        comment_type = types_to_test[i % len(types_to_test)]
        icp_score = int(((c.get("gate_results") or {}).get("icp") or {}).get("total") or 0)
        author = f"{c.get('author_name') or 'Unknown'} ({c.get('author_title') or '?'} at {c.get('author_company') or '?'})"

        print()
        line()
        print(f"## CANDIDATE {i+1}  Type {comment_type}  ICP {icp_score}")
        print(f"## Author: {author}")
        line()
        print()
        print("ORIGINAL post:")
        print("  " + (c.get("post_text") or "")[:600])
        print()

        if c.get("comment_text"):
            print("PREVIOUS draft (old prompt):")
            print("  " + c["comment_text"])
            print()

        try:
            new_text = draft_comment(
                cofounder_name=cofounder.get("display_name") or "",
                cofounder_tone=voice.get("tone_description") or "",
                cofounder_examples=voice.get("examples") or [],
                post_text=c.get("post_text") or "",
                author_name=c.get("author_name"),
                author_title=c.get("author_title"),
                author_company=c.get("author_company"),
                icp_score=icp_score,
                comment_type=comment_type,
                source_classification=c.get("source_classification") or "A",
            )
        except Exception as err:
            print(f"  drafter error: {err}")
            continue

        result = validate_comment(new_text, comment_type)
        print(f"NEW draft (rebuilt prompt, Type {comment_type}):")
        for ln in new_text.splitlines() or [new_text]:
            print(f"  {ln}")
        print()
        print(f"VALIDATOR: ok={result.ok}  reason={result.reason}")

    # Reply drafter — public reply-back + DM hot + DM warm
    print()
    line("═")
    print("REPLY DRAFTER — three contexts on a synthetic reply")
    line("═")
    sample_post = cands[0].get("post_text") or ""
    sample_our_comment = cands[0].get("comment_text") or ""
    synth_reply = (
        "Strong perspective. We are seeing the same thing — the teams that "
        "tighten the call list before launch close noticeably faster."
    )
    cofounder_first = (cofounder.get("display_name") or "").strip().split(" ", 1)[0] or "Alex"

    for ctx in ("PUBLIC_REPLY_BACK", "POST_CR_DM_HOT", "POST_CR_DM_WARM"):
        print()
        line()
        print(f"## CONTEXT: {ctx}")
        line()
        try:
            text, rtype = draft_reply(
                reply_context=ctx,
                cofounder_name=cofounder.get("display_name") or "",
                cofounder_tone=voice.get("tone_description") or "",
                cofounder_first_name=cofounder_first,
                cofounder_calendly_url=cofounder.get("calendly_url") or "https://calendly.com/example/intro",
                author_name=cands[0].get("author_name"),
                author_title=cands[0].get("author_title"),
                author_company=cands[0].get("author_company"),
                original_post=sample_post,
                our_comment=sample_our_comment,
                their_reply=synth_reply,
                they_are_post_author=True,
                prospect_name=cands[0].get("author_name"),
            )
        except Exception as err:
            print(f"  reply_drafter error: {err}")
            continue
        for ln in text.splitlines() or [text]:
            print(f"  {ln}")
        print(f"\n  type={rtype}")

    # CR-note generator
    print()
    line("═")
    print("AUTO-CR NOTE — synthetic input")
    line("═")
    try:
        note = draft_cr_note(
            cofounder_name=cofounder.get("display_name") or "",
            sender_first=cofounder_first,
            prospect_name=cands[0].get("author_name") or "Unknown",
            prospect_title=cands[0].get("author_title"),
            prospect_company=cands[0].get("author_company"),
            trigger_action="Replied 2+ times on a shipped comment thread",
            specific_signal=f"the {cands[0].get('author_company') or 'product'} thread we engaged on",
        )
    except Exception as err:
        print(f"  cr-note error: {err}")
        return
    cr_result = validate_cr_note(note)
    print(f"  {note}")
    print(f"\n  chars={len(note)}  validator: ok={cr_result.ok}  reason={cr_result.reason}")
    print()
    line("═")


if __name__ == "__main__":
    main()
