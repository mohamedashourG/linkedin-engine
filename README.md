# LinkedIn Engagement Engine

A daily LinkedIn engagement engine for B2B operators. Operators manage 1–N LinkedIn accounts ("cofounders"), and the engine produces a daily slate of high-ICP comments to post manually.

> **Phase 1 status:** foundation only — monorepo + auth + docker-compose. The discovery / 4-gate filter / drafter / RULE 23 / reply monitor / harvester land in Phases 2–6.

---

## Tech stack

| Layer | Tool |
|---|---|
| Frontend | Next.js 15 (app router) + Tailwind + shadcn/ui + React Query |
| Backend | FastAPI 0.115 + Pydantic v2 + Motor |
| Workers | Celery 5 + Redis broker, Celery Beat for scheduling |
| Database | MongoDB |
| Cache/Queue | Redis |
| Auth | Custom FastAPI + JWT (pyjwt + bcrypt + httpOnly cookies) |

---

## Local development

### Prerequisites
- Docker + Docker Compose
- Node.js 20+ (for the frontend dev server)
- Python 3.12+ (only if you want to run the backend outside Docker)

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env if you want — defaults work for local dev.
```

### 2. Start backend stack (mongo, redis, backend, worker, beat)

```bash
cd infra
docker compose up --build
```

This starts five services:

| Service | Port | Purpose |
|---|---|---|
| `mongo` | 27017 | MongoDB |
| `redis` | 6379 | Redis (Celery broker + cache) |
| `backend` | 8000 | FastAPI on uvicorn (auto-reload) |
| `worker` | – | Celery worker |
| `beat` | – | Celery beat scheduler |

Sanity-check:

```bash
curl http://localhost:8000/healthz
# → {"status":"ok"}
```

### 3. Start frontend dev server

In a separate terminal:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:3000>. Unauthenticated visitors are redirected to `/login`; create an account at `/signup`.

---

## Project layout

```
linkedin-engine/
├── frontend/          Next.js 15 (app router)
│   ├── app/
│   │   ├── (auth)/    public: login + signup
│   │   └── (dashboard)/ protected: dashboard at /
│   ├── components/    UI primitives + shared
│   ├── lib/           api client + auth helpers
│   └── middleware.ts  cookie-based protected-route guard
│
├── backend/           FastAPI + Celery
│   ├── app/
│   │   ├── auth/      JWT + bcrypt + signup/login/logout/me
│   │   ├── models/    Pydantic schemas (Phase 1: user only)
│   │   ├── routes/    (Phase 2+)
│   │   ├── services/  (Phase 2+)
│   │   ├── engine/    (Phase 3+)
│   │   ├── main.py    FastAPI app + lifespan + CORS
│   │   ├── config.py  pydantic-settings
│   │   ├── database.py Motor client + index ensure
│   │   └── celery_app.py Celery wiring
│   └── tests/
│
└── infra/
    └── docker-compose.yml  mongo + redis + backend + worker + beat
```

---

## Auth flow

- `POST /api/auth/signup` — create user, set `access_token` httpOnly cookie
- `POST /api/auth/login` — verify password, set cookie
- `POST /api/auth/logout` — clear cookie
- `GET /api/auth/me` — return the authenticated user

The cookie is `httpOnly`, `SameSite=Lax`, and `Secure` only when `COOKIE_SECURE=true` (set in prod). Token is signed JWT (HS256), default 30-day lifetime.

The frontend uses a Next.js middleware to gate every non-auth route on the cookie's presence; the dashboard layout double-checks via a server-side cookie read so an expired or stripped cookie still bounces the user back to `/login`.

---

## Tests

```bash
docker compose exec backend pytest -q
```

`tests/test_auth.py` exercises signup → me → login → logout end-to-end against a real MongoDB.

---

## Build phases

- [x] **Phase 1** — monorepo + docker-compose + auth + login/signup
- [ ] **Phase 2** — onboarding wizard (product → cofounders → voice → calendly → schedule)
- [ ] **Phase 3** — engine core (discovery → verification → 4 gates → drafter → RULE 23 → email)
- [ ] **Phase 4** — reply monitor + auto-CR + reply digest
- [ ] **Phase 5** — pipeline kanban + EOD form + Calendly attribution + embedded harvester
- [ ] **Phase 6** — analytics + settings + Vercel/Railway deploy

---

## Locked decisions (non-negotiable)

See the spec for the full 23 — the load-bearing ones for v1:
- Operator-style multi-account model (cofounders are objects, not users).
- apidirect.io is the only LinkedIn read source. Posting is always manual.
- MongoDB only (no Postgres). Custom JWT auth (no Auth0 / Clerk).
- Free for v1, no Stripe.
- RULE 23 atomic postcondition (6 layers + HMAC + force-abort allowlist) lands in Phase 3.
