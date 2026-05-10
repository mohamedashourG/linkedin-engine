"""
Contact groups: create-with-group, list groups, bulk-assign, list filter,
active-group setter, and the discovery filter that scopes a run to one group.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import close_mongo_connection, connect_to_mongo, get_db
from app.main import app

EMAIL = "groups-test@example.com"
PASSWORD = "supersecret123"


async def _signup(client: AsyncClient) -> dict[str, str]:
    r = await client.post(
        "/api/auth/signup",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "name": "Groups Tester",
            "timezone": "America/New_York",
        },
    )
    assert r.status_code == 201, r.text
    return {k: v for k, v in r.cookies.items()}


async def _bulk_with_group(client, cookies, text, group=None):
    body: dict = {"text": text}
    if group:
        body["group"] = group
    r = await client.post("/api/contacts/bulk", json=body, cookies=cookies)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_with_group_filter_and_listing():
    """Bulk imports: explicit group wins, omitted group auto-mints a unique
    'Pasted list ...' label so contacts never fall into the ungrouped bucket
    by default."""
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup(client)

        # 2 explicit groups + 1 auto-named batch (no group passed)
        await _bulk_with_group(client, cookies, "Alice X, VP, X Co\nBob X, CHRO, X Co", group="Company X")
        await _bulk_with_group(client, cookies, "Carol Y, CFO, Y Inc", group="Company Y")
        auto_resp = await _bulk_with_group(client, cookies, "Dave Free, COO, Free Inc")
        auto_group = auto_resp["group"]
        assert auto_group  # auto-minted name, never None when something was inserted

        # Listing without filter returns all 4
        r = await client.get("/api/contacts/", cookies=cookies)
        all_contacts = r.json()
        assert len(all_contacts) == 4
        groups_on_docs = sorted(
            [c.get("group") for c in all_contacts],
            key=lambda x: (x is None, x or ""),
        )
        assert groups_on_docs == ["Company X", "Company X", "Company Y", auto_group]

        # Filter to an explicit group
        r = await client.get("/api/contacts/?group=Company%20X", cookies=cookies)
        x_only = r.json()
        assert len(x_only) == 2
        assert all(c["group"] == "Company X" for c in x_only)

        # Filter to the auto-minted group
        r = await client.get(
            f"/api/contacts/?group={auto_group}", cookies=cookies
        )
        auto_only = r.json()
        assert len(auto_only) == 1
        assert auto_only[0]["name"] == "Dave Free"

        # The ungrouped sentinel returns nothing — auto-naming guarantees
        # no contact lands without a group.
        r = await client.get("/api/contacts/?group=__ungrouped__", cookies=cookies)
        assert r.json() == []

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_groups_endpoint_summarizes_counts_and_active():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup(client)
        await _bulk_with_group(client, cookies, "Alice, VP\nBob, CHRO", group="Company X")
        # Auto-named batch (no group passed) → unique label, not ungrouped
        auto_resp = await _bulk_with_group(client, cookies, "Dave, COO")
        auto_group = auto_resp["group"]
        assert auto_group

        r = await client.get("/api/contacts/groups", cookies=cookies)
        body = r.json()
        names = [g["name"] for g in body["groups"]]
        # Real groups alpha, no ungrouped bucket because auto-naming covered it.
        assert names == sorted(names, key=lambda n: (n is None, (n or "").lower()))
        assert "Company X" in names
        assert auto_group in names
        assert None not in names  # nothing landed ungrouped
        counts = {g["name"]: g["count"] for g in body["groups"]}
        assert counts["Company X"] == 2
        assert counts[auto_group] == 1
        assert body["total_contacts"] == 3
        assert body["active_group"] is None

        # Set active group + re-fetch
        r = await client.put(
            "/api/contacts/active-group",
            json={"group": "Company X"},
            cookies=cookies,
        )
        assert r.status_code == 200
        assert r.json() == {"active_group": "Company X"}
        r = await client.get("/api/contacts/groups", cookies=cookies)
        assert r.json()["active_group"] == "Company X"

        # Clear it
        r = await client.put(
            "/api/contacts/active-group", json={"group": None}, cookies=cookies
        )
        assert r.json() == {"active_group": None}

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_group_assigns_and_clears():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup(client)
        await _bulk_with_group(client, cookies, "Alice, VP\nBob, CHRO\nCarol, COO")

        r = await client.get("/api/contacts/", cookies=cookies)
        ids = [c["_id"] for c in r.json()]
        assert len(ids) == 3

        # Assign first two to "VIP"
        r = await client.post(
            "/api/contacts/bulk-group",
            json={"ids": ids[:2], "group": "VIP"},
            cookies=cookies,
        )
        assert r.status_code == 200
        assert r.json() == {"updated": 2}

        # Re-list filtered
        r = await client.get("/api/contacts/?group=VIP", cookies=cookies)
        assert len(r.json()) == 2

        # Clear group on one of them
        r = await client.post(
            "/api/contacts/bulk-group",
            json={"ids": [ids[0]], "group": None},
            cookies=cookies,
        )
        assert r.json() == {"updated": 1}
        r = await client.get("/api/contacts/?group=VIP", cookies=cookies)
        assert len(r.json()) == 1

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_without_group_auto_generates_unique_name():
    """If the user pastes contacts without picking a group, the server mints
    one (timestamped) and returns it so the UI can drill straight in.
    Repeated imports collide-suffix to keep groups distinct."""
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup(client)

        # First batch — no group
        r1 = await client.post(
            "/api/contacts/bulk",
            json={"text": "Alice 1, VP\nBob 1, CHRO"},
            cookies=cookies,
        )
        body1 = r1.json()
        assert body1["inserted"] == 2
        # Auto-named, never null when something was inserted.
        assert isinstance(body1["group"], str) and body1["group"]
        assert body1["group"].startswith("Pasted list")

        # Second batch right after — must NOT collide
        r2 = await client.post(
            "/api/contacts/bulk",
            json={"text": "Alice 2, VP\nBob 2, CHRO"},
            cookies=cookies,
        )
        body2 = r2.json()
        assert body2["inserted"] == 2
        assert body2["group"]
        # Either same minute → collision suffix, or different minute → fresh stamp
        if body2["group"] == body1["group"]:
            pytest.fail("auto-name collision was not resolved")

        # Confirm both appear in the groups summary
        r = await client.get("/api/contacts/groups", cookies=cookies)
        groups = {g["name"] for g in r.json()["groups"] if g["name"]}
        assert body1["group"] in groups
        assert body2["group"] in groups

        # An explicit group still wins
        r3 = await client.post(
            "/api/contacts/bulk",
            json={"text": "Carol, COO", "group": "VIP"},
            cookies=cookies,
        )
        assert r3.json()["group"] == "VIP"

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_zero_inserts_returns_null_group():
    """If the bulk text parses to zero rows, the response carries group=null
    so the frontend doesn't drill into a phantom group."""
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup(client)
        r = await client.post(
            "/api/contacts/bulk",
            json={"text": "   \n  "},  # nothing parseable
            cookies=cookies,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["inserted"] == 0
        assert body["group"] is None

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


def test_discovery_ungrouped_sentinel_filters_to_no_group_seeds(monkeypatch):
    """active_contact_group='__ungrouped__' must scope discovery to seeds
    without a group label (group is None / missing / empty)."""
    from bson import ObjectId

    from app.engine.stages import discovery

    op_id = ObjectId()
    operator = {
        "_id": op_id,
        "active_contact_group": "__ungrouped__",
        "product_description": "x",
    }
    cofounder = {
        "_id": ObjectId(),
        "display_name": "test",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {"tone_description": "t" * 30, "examples": [{"post": "p", "comment": "c"}]},
    }

    seeds_in_db = [
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Grouped 1", "linkedin_url": "https://www.linkedin.com/in/g1",
         "group": "Company X"},
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Ungrouped 1", "linkedin_url": "https://www.linkedin.com/in/u1",
         "group": None},
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Ungrouped 2", "linkedin_url": "https://www.linkedin.com/in/u2"},  # no field at all
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Empty Group", "linkedin_url": "https://www.linkedin.com/in/e1",
         "group": ""},  # empty string also = ungrouped
    ]

    seeds_observed: list[dict] = []

    class _FakeColl:
        def find(self, *_a, **_kw):
            return iter(seeds_in_db)
        def insert_one(self, d):
            class _R: inserted_id = ObjectId()
            return _R()
        def update_many(self, *_a, **_kw):
            class _R: modified_count = 0
            return _R()
        def aggregate(self, *_a, **_kw):
            return iter([])
        def count_documents(self, *_a, **_kw):
            return 0
        def find_one(self, *_a, **_kw):
            return None

    class _FakeDB:
        def __init__(self):
            self.discovery_seeds = _FakeColl()
            self.candidates = _FakeColl()
            self.audit_records = _FakeColl()
            self.keyword_history = _FakeColl()
            self.exhaustion_ledger = _FakeColl()
            self.crustdata_inbox = _FakeColl()
            self.profiles = _FakeColl()

    db = _FakeDB()

    def fake_contact_unipile(*_a, seeds, **_kw):
        seeds_observed.extend(seeds)
        return 0

    monkeypatch.setattr(discovery, "_run_contact_seeds_unipile", fake_contact_unipile)

    discovery.discover_for_operator(
        db, operator=operator, cofounders=[cofounder], slate_run_id=ObjectId()
    )

    names = sorted([s["extracted_name"] for s in seeds_observed])
    # All three "ungrouped" variants (None, missing field, empty string) pass;
    # the named-group seed is filtered out.
    assert names == ["Empty Group", "Ungrouped 1", "Ungrouped 2"]


def test_discovery_active_group_filters_seeds(monkeypatch):
    """Discovery's contact_seeds list is filtered by operator.active_contact_group."""
    from bson import ObjectId

    from app.engine.stages import discovery

    # Build a fake operator with active_contact_group="Company X"
    op_id = ObjectId()
    operator = {
        "_id": op_id,
        "active_contact_group": "Company X",
        "product_description": "x",
    }
    cofounder = {
        "_id": ObjectId(),
        "display_name": "test",
        "active": True,
        "unipile_account_id": "acc",
        "voice_profile": {"tone_description": "t" * 30, "examples": [{"post": "p", "comment": "c"}]},
    }

    seeds_in_db = [
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Alice X", "linkedin_url": "https://www.linkedin.com/in/alice-x",
         "group": "Company X"},
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Bob X", "linkedin_url": "https://www.linkedin.com/in/bob-x",
         "group": "Company X"},
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Carol Y", "linkedin_url": "https://www.linkedin.com/in/carol-y",
         "group": "Company Y"},
        {"source": "manual", "operator_id": op_id, "status": "pending",
         "expires_at": discovery.utcnow() + discovery.timedelta(days=10),
         "extracted_name": "Dave Free", "linkedin_url": "https://www.linkedin.com/in/dave-free",
         "group": None},
    ]

    seeds_observed: list[dict] = []

    class _FakeColl:
        def find(self, q, *_a, **_kw):
            return iter(seeds_in_db)
        def insert_one(self, d):
            class _R: inserted_id = ObjectId()
            return _R()
        def update_many(self, *_a, **_kw):
            class _R: modified_count = 0
            return _R()
        def aggregate(self, *_a, **_kw):
            return iter([])
        def count_documents(self, *_a, **_kw):
            return 0
        def find_one(self, *_a, **_kw):
            return None

    class _FakeDB:
        def __init__(self):
            self.discovery_seeds = _FakeColl()
            self.candidates = _FakeColl()
            self.audit_records = _FakeColl()
            self.keyword_history = _FakeColl()
            self.exhaustion_ledger = _FakeColl()
            self.crustdata_inbox = _FakeColl()
            self.profiles = _FakeColl()

    db = _FakeDB()

    # Stub _run_contact_seeds_unipile to capture which seeds it received.
    def fake_contact_unipile(*args, seeds, **kwargs):
        seeds_observed.extend(seeds)
        return 0

    monkeypatch.setattr(discovery, "_run_contact_seeds_unipile", fake_contact_unipile)

    # Run discovery; we don't care about return value, only the seeds passed in.
    discovery.discover_for_operator(
        db, operator=operator, cofounders=[cofounder], slate_run_id=ObjectId()
    )

    names = sorted([s["extracted_name"] for s in seeds_observed])
    assert names == ["Alice X", "Bob X"], (
        "active_contact_group=Company X must scope discovery to those 2 seeds only"
    )
