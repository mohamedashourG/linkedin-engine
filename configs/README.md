## configs/

Per-client overlays consumed by `app.services.client_config`. One directory per
client slug; the slug is set on the user document (`users.client_slug`) and
defaults to `"glnk"` when absent.

Files (all optional; missing files fall back to engine defaults):

```
configs/<slug>/
  client.json            # slug, display_name, ban-list for client name in CR notes
  icp_rubric.json        # 4-axis tiered scoring overlay for icp_scoring gate
  keyword_pools.json     # tier_1 / tier_2 / tier_3 + title_industry pools
  voice_profiles.json    # per-cofounder tone hints (overlays cofounder.voice_profile)
  drafter_guardrails.json  # banned phrases, anchors, CTAs the drafter prompt must respect
```

When adding a new client: copy `configs/glnk/` to `configs/<new-slug>/`, edit
the JSON files, and set `users.client_slug = "<new-slug>"` for that operator.
No deploy needed for content edits — the loader caches by slug + mtime.
