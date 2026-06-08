"""
main.py

FastAPI application entry point.

Startup sequence (lifespan):
  1. setup_logging()   — configure root logger from LOG_LEVEL env var
  2. create_tables()   — create DB tables if they don't exist (SQLite or PostgreSQL)
  3. discover_tools()  — import all files in tools/, register @register_tool functions

To run:
  uvicorn main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routes import router
from config import settings
from core.logger import setup_logging
from db.models import create_tables
from tools.registry import discover_tools


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    create_tables()
    discover_tools(domain=settings.domain_pack)
    yield


app = FastAPI(
    title="Hybrid AI Operations Agent",
    description=(
        "Domain-agnostic AI agent for small business operations automation. "
        "Add a tool domain: drop a file in tools/, decorate with @register_tool. "
        "Zero core changes required."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)

# Serve the single-page frontend at /
# Mount last so API routes take precedence.
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
