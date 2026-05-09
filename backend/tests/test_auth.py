"""
Smoke tests for the auth flow. Run with:
    docker compose exec backend pytest -q
or locally with mongo+redis up.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import connect_to_mongo, get_db, close_mongo_connection
from app.main import app


@pytest.mark.asyncio
async def test_signup_login_me_logout():
    await connect_to_mongo()
    db = get_db()
    await db.users.delete_many({"email": "test@example.com"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/signup",
            json={
                "email": "test@example.com",
                "password": "supersecret123",
                "name": "Test User",
                "timezone": "America/New_York",
            },
        )
        assert r.status_code == 201
        cookies = r.cookies

        r = await client.get("/api/auth/me", cookies=cookies)
        assert r.status_code == 200
        assert r.json()["email"] == "test@example.com"

        r = await client.post(
            "/api/auth/login",
            json={"email": "test@example.com", "password": "supersecret123"},
        )
        assert r.status_code == 200

        r = await client.post("/api/auth/logout", cookies=cookies)
        assert r.status_code == 204

    await db.users.delete_many({"email": "test@example.com"})
    await close_mongo_connection()
