"""
CLI entry point for the Action Normalizer pipeline.

Usage:
  python run_normalizer.py                                          # reads output/output.json → output/normalized_output.json
  python run_normalizer.py output/output.json                      # custom input file
  python run_normalizer.py output/output.json result.json          # custom input and output
  python run_normalizer.py output/output.json result.json --meeting-date 2026-03-05

The input file must be a JSON array of action objects (the output of run_extractor.py).
The --meeting-date flag sets the reference date for relative deadline resolution
(e.g. "after the meeting" → that date).  Defaults to today.
"""
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Ensure the project root is on the path regardless of where the script is run from
sys.path.insert(0, str(Path(__file__).parent))

from src.action_normalizer.workflow import normalize_actions

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%H:%M:%S"


def _setup_logging(log_file: str = "output/normalizer_log.txt") -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass


logger = logging.getLogger(__name__)


def _parse_args() -> tuple[str, str, str | None]:
    """Return (input_file, output_file, meeting_date_or_None)."""
    args = sys.argv[1:]
    input_file = "output/output.json"
    output_file = "output/normalized_output.json"
    meeting_date = None

    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("--meeting-date", "--date") and i + 1 < len(args):
            meeting_date = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            i += 1
        else:
            positional.append(args[i])
            i += 1

    if positional:
        input_file = positional[0]
    if len(positional) >= 2:
        output_file = positional[1]

    return input_file, output_file, meeting_date


def main() -> None:
    _setup_logging()

    input_file, output_file, meeting_date = _parse_args()

    if meeting_date is None:
        meeting_date = date.today().isoformat()
        logger.info("No --meeting-date supplied; defaulting to today (%s)", meeting_date)

    # Load raw actions
    input_path = Path(input_file)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_file)
        sys.exit(1)

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            raw_actions = json.load(f)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in %s: %s", input_file, exc)
        sys.exit(1)

    if not isinstance(raw_actions, list):
        logger.error(
            "Expected a JSON array of action objects in %s, got %s",
            input_file,
            type(raw_actions).__name__,
        )
        sys.exit(1)

    logger.info(
        "Loaded %d action(s) from %s (meeting_date=%s)", len(raw_actions), input_file, meeting_date
    )

    # Run normalizer pipeline
    try:
        normalized = normalize_actions(raw_actions, meeting_date=meeting_date)
    except Exception as exc:
        logger.error("Normalizer pipeline failed: %s", exc, exc_info=True)
        sys.exit(1)

    # Write output
    output_path = Path(output_file)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("Could not write output file %s: %s", output_file, exc)
        sys.exit(1)

    logger.info("Done. %d normalised action(s) written to %s", len(normalized), output_file)

    # Print a summary table to stdout
    print(f"\n{'─' * 90}")
    print(f"{'#':<3}  {'TOOL':<22}  {'VERB':<14}  {'DEADLINE':<12}  {'ASSIGNEE':<10}  DESCRIPTION")
    print(f"{'─' * 90}")
    for idx, action in enumerate(normalized, 1):
        tool = action.get("tool_type", "?")
        verb = action.get("verb", "?")
        dl = action.get("normalized_deadline") or "—"
        assignee = (action.get("assignee") or "—")[:10]
        desc = action.get("description", "")[:45]
        parent = " (split)" if action.get("parent_id") else ""
        print(f"{idx:<3}  {tool:<22}  {verb:<14}  {dl:<12}  {assignee:<10}  {desc}{parent}")
    print(f"{'─' * 90}\n")


if __name__ == "__main__":
    main()
