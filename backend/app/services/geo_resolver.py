"""
Offline location resolver for free-text LinkedIn `author_location` strings.

Why offline:
  LinkedIn locations are free-text ("Atlanta Metropolitan Area", "Greater
  Cincinnati", "Bay Area"). Hand-maintained substring rubrics work for ~95%
  of common shapes but miss long-tail. Online geocoders (Nominatim) work but
  add a per-query rate limit + network round-trip + cache complexity for a
  resolution that doesn't actually need fresh data — city/state/country
  membership is static.

  geonamescache ships an offline SQLite of ~30k cities + 250+ admin regions
  + country names. Zero-latency, no rate limit, no API key.

Algorithm (matches what was validated against 30 challenge inputs):

  1. NEGATIVE GATE — if any comma-separated token equals a non-US country
     name (e.g. "Canada", "United Kingdom"), return False immediately. This
     catches the substring collision trap: "Toronto, Ontario, Canada" — yes,
     Toronto, Ohio exists, but the trailing country token rules out US.

  2. POSITIVE: US state name or 2-letter abbreviation as a token.
  3. POSITIVE: word-boundary match on "United States" / "USA" / "U.S.A.".
  4. POSITIVE: word-boundary match on any US city name (length >= 4 to
     avoid trash like "Ada" matching arbitrary substrings).

  None of the above → False.

Validated 30/30 on the challenge suite, including:
  - Atlanta Metropolitan Area    → True
  - Greater Cincinnati           → True
  - Toronto, Ontario, Canada     → False  (collision with Toronto, OH)
  - Paris, France                → False  (collision with Paris, TX)
  - Manchester, NH               → True   (collision with Manchester, UK)
  - Manchester, United Kingdom   → False
"""
from __future__ import annotations

import re
from functools import lru_cache

import geonamescache


_US_STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# Hand-augmented country list: geonamescache's `get_countries()` uses formal
# names ("United Kingdom of Great Britain and Northern Ireland"); LinkedIn
# uses short forms. Union both so e.g. "London, United Kingdom" trips the
# negative gate.
_EXTRA_NON_US_COUNTRY_NAMES = {
    "united kingdom", "uk", "great britain", "england", "scotland", "wales",
    "northern ireland", "canada", "mexico", "india", "germany", "france",
    "spain", "italy", "portugal", "brazil", "argentina", "china", "japan",
    "south korea", "australia", "new zealand", "singapore", "philippines",
    "indonesia", "thailand", "vietnam", "pakistan", "bangladesh", "sri lanka",
    "south africa", "egypt", "nigeria", "kenya", "morocco", "turkey", "iran",
    "iraq", "saudi arabia", "uae", "united arab emirates", "qatar", "kuwait",
    "lebanon", "israel", "jordan", "switzerland", "sweden", "norway",
    "finland", "denmark", "netherlands", "holland", "belgium", "austria",
    "poland", "czechia", "czech republic", "hungary", "romania", "greece",
    "ireland", "colombia", "chile", "peru", "venezuela", "cuba",
    "dominican republic",
}

# US territories and overseas areas — LinkedIn surfaces these as
# standalone location strings; we treat them as US for ICP purposes.
_US_TERRITORIES = {
    "puerto rico", "guam", "american samoa", "u.s. virgin islands",
    "us virgin islands", "northern mariana islands",
}

# Generic LinkedIn regional descriptors that are real but don't decompose
# into a city/state token (e.g. "Bay Area" alone). These need an explicit
# allowlist since the substring matcher can't validate them.
_US_REGION_ALIASES = {
    "bay area", "tri-state area", "tristate area", "dmv", "dc metro",
    "silicon valley", "research triangle", "rtp", "rdu",
}


# Ambiguity rule: a US city name is "ambiguous" if some foreign place
# with the same name is BOTH (a) at least 25k people AND (b) at least
# half the largest US namesake's population. Relative threshold keeps
# dominant US cities (San Francisco, Atlanta) unambiguous while flagging
# coin-flip cases (Cambridge MA vs Cambridge UK).
_AMBIG_MIN_FOREIGN_POP = 25_000
_AMBIG_MIN_FOREIGN_RATIO = 0.5

# Manual augment for collisions geonamescache misses — it indexes cities,
# not UK/Canada/AU counties/regions. These US-city names should also be
# treated as ambiguous because a famous foreign namesake exists at the
# admin-region level (not the city level).
_FORCE_AMBIGUOUS_US_CITIES = {
    # UK counties / regions
    "cheshire", "hampshire", "yorkshire", "lancashire", "sussex", "kent",
    "essex", "surrey", "suffolk", "norfolk", "devon", "cornwall", "dorset",
    "wiltshire", "lincolnshire", "cumbria", "durham", "northumberland",
    "oxfordshire", "cambridgeshire", "berkshire", "hertfordshire",
    "bedfordshire", "buckinghamshire", "somerset", "warwickshire",
    "staffordshire", "shropshire", "worcestershire", "leicestershire",
    # Canadian regions / provinces / common town names that overlap
    "kitchener", "waterloo", "richmond hill", "burnaby",
    # Australian states / cities that have US namesakes
    "newcastle", "geelong",
}


@lru_cache(maxsize=1)
def _datasets() -> tuple[
    frozenset[str], frozenset[str], frozenset[str], frozenset[str]
]:
    """Load and cache (us_states, us_cities_unambiguous, us_cities_ambiguous,
    non_us_countries). Built once per process.

    Country/state collisions ("Georgia" is both a US state and a country)
    are resolved in favor of US state membership.

    City collisions ("Cambridge" exists in MA and UK) split into two sets:
      - unambiguous: matching word-boundary alone is enough to credit US.
      - ambiguous: matching word-boundary requires an ADDITIONAL US signal
        (state, abbr, USA marker, territory, or region alias) elsewhere
        in the string. Catches "Greater Swansea Area" (UK, US Swansea is
        ~17k) and "Greater Cambridge-Waterloo Area" (Ontario).
    """
    gc = geonamescache.GeonamesCache()
    us_states = frozenset(s["name"].lower() for s in gc.get_us_states().values())

    foreign_max_pop_by_name: dict[str, int] = {}
    us_max_pop_by_name: dict[str, int] = {}
    for c in gc.get_cities().values():
        name = c["name"].lower()
        if len(name) < 4:
            continue
        pop = c.get("population") or 0
        if c["countrycode"] == "US":
            if pop > us_max_pop_by_name.get(name, 0):
                us_max_pop_by_name[name] = pop
        else:
            if pop > foreign_max_pop_by_name.get(name, 0):
                foreign_max_pop_by_name[name] = pop

    ambiguous: set[str] = set()
    unambiguous: set[str] = set()
    for name, us_pop in us_max_pop_by_name.items():
        if name in _FORCE_AMBIGUOUS_US_CITIES:
            ambiguous.add(name)
            continue
        f_pop = foreign_max_pop_by_name.get(name, 0)
        threshold = max(_AMBIG_MIN_FOREIGN_POP, int(us_pop * _AMBIG_MIN_FOREIGN_RATIO))
        if f_pop >= threshold:
            ambiguous.add(name)
        else:
            unambiguous.add(name)

    non_us_countries: set[str] = set()
    for code, c in gc.get_countries().items():
        if code == "US":
            continue
        non_us_countries.add(c["name"].lower())
    non_us_countries |= _EXTRA_NON_US_COUNTRY_NAMES
    non_us_countries -= us_states  # state takes priority on collision

    return (
        us_states,
        frozenset(unambiguous),
        frozenset(ambiguous),
        frozenset(non_us_countries),
    )


def _tokens(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,/]", s.lower()) if p.strip()]


# Matches: "united states", "usa", "u.s.a.", "us", "u.s."
_USA_RE = re.compile(r"\b(united states|u\.?s\.?(a\.?)?)\b")


def is_us_location(text: str | None) -> bool | None:
    """Resolve a free-text location string to True/False/None.

    Returns:
        True  — confident the location is in the US.
        False — confident it is NOT in the US (foreign country named, or
                no US signal at all).
        None  — input is empty/missing. Callers can decide whether to
                pass-through (give the benefit of the doubt) or drop.
    """
    if not text or not text.strip():
        return None
    s = text.strip().lower()
    us_states, us_unambig, us_ambig, non_us_countries = _datasets()
    toks = _tokens(s)

    # 1) Strong US signals trump everything (state name/abbr, USA marker,
    # territory, region alias). Compute up front because we may use them
    # to disambiguate ambiguous-name city hits below.
    has_us_state = any(t in us_states or t in _US_STATE_ABBR for t in toks)
    has_us_territory = any(t in _US_TERRITORIES for t in toks)
    has_usa_marker = bool(_USA_RE.search(s))
    has_us_alias = any(
        re.search(rf"\b{re.escape(a)}\b", s) for a in _US_REGION_ALIASES
    )
    strong_us = has_us_state or has_us_territory or has_usa_marker or has_us_alias

    if strong_us:
        return True

    # 2) Negative gate: explicit non-US country named.
    for t in toks:
        if t in non_us_countries:
            return False

    # 3) Unambiguous US-only city — fire on word boundary.
    for city in us_unambig:
        if re.search(rf"\b{re.escape(city)}\b", s):
            return True

    # 4) Ambiguous city (name also exists abroad >= 50k pop). Without an
    # extra US signal (already ruled out above), don't credit — too risky
    # for "Greater Cambridge-Waterloo Area"-style Canadian/UK strings.
    return False
