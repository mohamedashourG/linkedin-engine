"""
Tests for contact deletion: single, bulk-by-ids, and delete-all.

These hit the live backend (mongo + auth) the same way test_auth.py does.
Run with `docker compose exec backend pytest tests/test_contacts_delete.py -q`.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import close_mongo_connection, connect_to_mongo, get_db
from app.main import app

EMAIL = "contacts-delete@example.com"
PASSWORD = "supersecret123"


async def _signup_and_get_cookies(client: AsyncClient) -> dict[str, str]:
    r = await client.post(
        "/api/auth/signup",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "name": "Contacts Tester",
            "timezone": "America/New_York",
        },
    )
    assert r.status_code == 201, r.text
    return {k: v for k, v in r.cookies.items()}


async def _seed_contacts(client: AsyncClient, cookies: dict[str, str]) -> list[str]:
    text = "\n".join(
        [
            "Alice Example, VP HR, St Example Health",
            "Bob Example, CHRO, Memorial Health",
            "Carol Example, COO, Riverside Hospital",
        ]
    )
    r = await client.post("/api/contacts/bulk", json={"text": text}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3
    r = await client.get("/api/contacts/", cookies=cookies)
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) == 3
    return [d["_id"] for d in docs]


@pytest.mark.asyncio
async def test_delete_single_contact():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup_and_get_cookies(client)
        ids = await _seed_contacts(client, cookies)

        r = await client.delete(f"/api/contacts/{ids[0]}", cookies=cookies)
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        # Confirm only that one is gone.
        r = await client.get("/api/contacts/", cookies=cookies)
        remaining = {d["_id"] for d in r.json()}
        assert ids[0] not in remaining
        assert ids[1] in remaining and ids[2] in remaining

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_delete_by_ids():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup_and_get_cookies(client)
        ids = await _seed_contacts(client, cookies)

        r = await client.post(
            "/api/contacts/bulk-delete",
            json={"ids": [ids[0], ids[1]]},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 2

        r = await client.get("/api/contacts/", cookies=cookies)
        remaining = [d["_id"] for d in r.json()]
        assert remaining == [ids[2]]

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_delete_empty_ids_is_noop():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup_and_get_cookies(client)
        await _seed_contacts(client, cookies)

        r = await client.post(
            "/api/contacts/bulk-delete", json={"ids": []}, cookies=cookies
        )
        assert r.status_code == 200
        assert r.json() == {"deleted": 0}

        r = await client.get("/api/contacts/", cookies=cookies)
        assert len(r.json()) == 3

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_delete_all_contacts():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": EMAIL})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = await _signup_and_get_cookies(client)
        await _seed_contacts(client, cookies)

        r = await client.delete("/api/contacts/", cookies=cookies)
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 3

        r = await client.get("/api/contacts/", cookies=cookies)
        assert r.json() == []

    await db.users.delete_many({"email": EMAIL})
    await close_mongo_connection()


@pytest.mark.asyncio
async def test_bulk_delete_only_affects_owner():
    """Operator A's bulk-delete must NOT touch operator B's contacts."""
    await connect_to_mongo()
    db = get_db()
    other_email = "contacts-delete-other@example.com"
    await db.users.delete_many({"email": EMAIL})
    await db.users.delete_many({"email": other_email})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Operator A
        cookies_a = await _signup_and_get_cookies(client)
        ids_a = await _seed_contacts(client, cookies_a)

        # Operator B
        r = await client.post(
            "/api/auth/signup",
            json={
                "email": other_email,
                "password": PASSWORD,
                "name": "Other",
                "timezone": "America/New_York",
            },
        )
        assert r.status_code == 201
        cookies_b = {k: v for k, v in r.cookies.items()}
        r = await client.post(
            "/api/contacts/bulk",
            json={"text": "Diana Other, CFO, Other Co"},
            cookies=cookies_b,
        )
        assert r.json()["inserted"] == 1

        # Operator B tries to delete Operator A's ids — should report 0.
        r = await client.post(
            "/api/contacts/bulk-delete",
            json={"ids": ids_a},
            cookies=cookies_b,
        )
        assert r.status_code == 200
        assert r.json()["deleted"] == 0

        # Operator A's contacts still intact.
        r = await client.get("/api/contacts/", cookies=cookies_a)
        assert len(r.json()) == 3

        # Operator B can wipe their own.
        r = await client.delete("/api/contacts/", cookies=cookies_b)
        assert r.status_code == 200
        assert r.json()["deleted"] == 1

        # Operator A still untouched.
        r = await client.get("/api/contacts/", cookies=cookies_a)
        assert len(r.json()) == 3

    await db.users.delete_many({"email": EMAIL})
    await db.users.delete_many({"email": other_email})
    await close_mongo_connection()
