"""Celery application factory.

Broker and result backend both use Redis. Three dedicated queues keep agent
types independent so they can be scaled separately:
  - extractor
  - normalizer
  - executor

Start workers with:
    celery -A worker.celery_app worker --concurrency=4 -Q extractor,normalizer,executor
"""
import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "agent_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["worker.tasks"],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Timezone
    timezone="UTC",
    enable_utc=True,
    # Reliability
    task_acks_late=True,           # ack only after the task finishes (safe re-queue on crash)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # one task at a time per worker slot
    # Result TTL: keep results for 24 h so the API can poll status
    result_expires=86400,
    # Route each agent type to its own queue
    task_routes={
        "worker.tasks.run_extractor_task": {"queue": "extractor"},
        "worker.tasks.run_normalizer_task": {"queue": "normalizer"},
        "worker.tasks.run_executor_task": {"queue": "executor"},
    },
    # Default queue fallback
    task_default_queue="extractor",
)
