"""
Pipeline runner for API runs: extractor → normalizer → executor.

Runs synchronously and calls an emit callback for each SSE event.
The API runs this in a thread and wires emit to an asyncio.Queue via call_soon_threadsafe.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _emit(emit_cb: Callable[[str, dict], None], event_type: str, data: dict[str, Any]) -> None:
    """Helper to emit an SSE event."""
    try:
        emit_cb(event_type, data)
    except Exception as e:
        logger.warning("Emit callback error: %s", e)


def run_pipeline_sync(
    transcript_path: str,
    meeting_date: str | None,
    language: str | None,
    emit_cb: Callable[[str, dict], None],
    *,
    dry_run: bool = True,
    contacts_path: str | None = None,
) -> None:
    """
    Run the extractor → normalizer → executor pipeline and emit SSE events.

    emit_cb(event_type, data) is called from this thread; the API layer must
    use call_soon_threadsafe to put events on an asyncio.Queue.
    """
    try:
        import sys
        api_dir = Path(__file__).resolve().parent
        project_root = api_dir.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

        from src.action_extractor.main import load_transcript
        from src.action_extractor.workflow import extract_actions_with_progress
        from src.action_normalizer.workflow import normalize_actions_with_progress
        from src.action_executor.workflow import execute_actions_with_progress
    except Exception as e:
        _emit(emit_cb, "error", {"message": str(e), "code": "import_error"})
        return

    # --- EXTRACTOR ---
    _emit(emit_cb, "progress", {
        "agent": "extractor",
        "step": "load_transcript",
        "status": "running",
    })
    try:
        transcript = load_transcript(transcript_path)
    except Exception as e:
        _emit(emit_cb, "error", {"message": str(e), "agent": "extractor", "step": "load_transcript"})
        return

    _emit(emit_cb, "step_done", {"agent": "extractor", "step": "load_transcript"})

    try:
        actions = extract_actions_with_progress(transcript, emit_cb)
    except Exception as e:
        logger.exception("Extractor failed")
        _emit(emit_cb, "error", {"message": str(e), "agent": "extractor", "step": "extraction"})
        return

    logger.info("Extractor done: %d action(s) -> %s", len(actions), json.dumps(actions, default=str, ensure_ascii=False))

    _emit(emit_cb, "agent_done", {"agent": "extractor"})

    # --- NORMALIZER ---
    _emit(emit_cb, "progress", {
        "agent": "normalizer",
        "step": "deadline_normalizer",
        "status": "running",
    })
    try:
        normalized = normalize_actions_with_progress(actions, emit_cb, meeting_date=meeting_date or None)
    except Exception as e:
        logger.exception("Normalizer failed")
        _emit(emit_cb, "error", {"message": str(e), "agent": "normalizer", "step": "normalize"})
        return

    logger.info("Normalizer done: %d action(s) -> %s", len(normalized), json.dumps(normalized, default=str, ensure_ascii=False))
    _emit(emit_cb, "agent_done", {"agent": "normalizer"})

    # --- EXECUTOR ---
    _emit(emit_cb, "progress", {
        "agent": "executor",
        "step": "contact_resolver",
        "status": "running",
    })
    try:
        results = execute_actions_with_progress(
            normalized,
            emit_cb,
            dry_run=dry_run,
            contacts_path=contacts_path,
        )
    except Exception as e:
        logger.exception("Executor failed")
        _emit(emit_cb, "error", {"message": str(e), "agent": "executor", "step": "execute"})
        return

    logger.info("Executor done: %d result(s) -> %s", len(results), json.dumps(results, default=str, ensure_ascii=False))
    _emit(emit_cb, "agent_done", {"agent": "executor"})
    _emit(emit_cb, "run_complete", {
        "summary": {
            "actions_extracted": len(actions),
            "actions_normalized": len(normalized),
            "actions_executed": len(results),
        },
    })
