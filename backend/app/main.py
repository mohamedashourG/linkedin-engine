from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import router as auth_router
from app.config import settings
from app.database import close_mongo_connection, connect_to_mongo
from app.routes.analytics import router as analytics_router
from app.routes.contacts import router as contacts_router
from app.routes.crustdata import router as crustdata_router
from app.routes.eod import router as eod_router
from app.routes.manual_comments import router as manual_comments_router
from app.routes.onboarding import router as onboarding_router
from app.routes.pipeline import router as pipeline_router
from app.routes.replies import router as replies_router
from app.routes.settings import router as settings_router
from app.routes.slate import router as slate_router
from app.routes.unipile_pool import router as unipile_pool_router
from app.routes.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    yield
    await close_mongo_connection()


app = FastAPI(
    title="LinkedIn Engagement Engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.app_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(onboarding_router)
app.include_router(slate_router)
app.include_router(contacts_router)
app.include_router(replies_router)
app.include_router(eod_router)
app.include_router(pipeline_router)
app.include_router(webhooks_router)
app.include_router(analytics_router)
app.include_router(settings_router)
app.include_router(crustdata_router)
app.include_router(manual_comments_router)
app.include_router(unipile_pool_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
