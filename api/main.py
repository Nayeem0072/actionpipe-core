"""FastAPI application — APIs live under the api folder."""
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import runs as runs_routes

# Ensure api loggers (e.g. api.pipeline) output to console when running under uvicorn
_api_logger = logging.getLogger("api")
_api_logger.setLevel(logging.INFO)
if not _api_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _api_logger.addHandler(_handler)

app = FastAPI(
    title="Agent AI API",
    description="ActionPipe and other APIs.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs_routes.router)
