"""Main entry point for LangGraph action item extraction."""
import json
import logging
import sys
from pathlib import Path

# Add parent directory to path for config import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

# Handle both direct execution and module import
try:
    from .workflow import extract_actions
except ImportError:
    # If relative import fails, try absolute
    from src.action_extractor.workflow import extract_actions

# Log file name for LangGraph process
LOG_FILE = "output/output_log.txt"

# Configure logging to console (stderr) and to output_log file
_log_format = "%(asctime)s [%(levelname)s] %(message)s"
_log_datefmt = "%H:%M:%S"

def _setup_logging():
    """Configure root logger to write to both stderr and output_log."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers if main is run multiple times (e.g. in tests)
    if root.handlers:
        return
    formatter = logging.Formatter(_log_format, datefmt=_log_datefmt)
    # Console
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)
    # File (created when process runs; full log when process is done)
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass  # If we can't write output_log, continue with console only

logger = logging.getLogger(__name__)


def load_transcript(input_file: str) -> str:
    """
    Load transcript from input file.
    Supports both .txt (plain text) and .json (with transcript_raw field) formats.
    
    Args:
        input_file: Path to input file (.txt or .json)
        
    Returns:
        Transcript text as string
    """
    input_path = Path(input_file)
    
    if input_path.suffix.lower() == '.txt':
        # Plain text file - read entire content as transcript
        logger.info("Reading transcript from plain text file: %s", input_file)
        with open(input_file, 'r', encoding='utf-8') as f:
            transcript = f.read()
        return transcript.strip()
    
    elif input_path.suffix.lower() == '.json':
        # JSON file - extract transcript_raw field
        logger.info("Reading transcript from JSON file: %s", input_file)
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if "transcript_raw" not in data:
            raise ValueError("Input JSON must contain 'transcript_raw' field")
        
        return data["transcript_raw"]
    
    else:
        # Try to auto-detect: if it's valid JSON, treat as JSON; otherwise as text
        logger.info("Auto-detecting file format for: %s", input_file)
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        try:
            # Try parsing as JSON first
            data = json.loads(content)
            if "transcript_raw" in data:
                return data["transcript_raw"]
            else:
                raise ValueError("JSON file must contain 'transcript_raw' field")
        except json.JSONDecodeError:
            # Not JSON, treat as plain text
            logger.info("File is not JSON, treating as plain text transcript")
            return content.strip()


def main():
    """CLI entry point for LangGraph action extraction."""
    _setup_logging()

    # Default files
    default_input = "input/input.txt"
    default_output = "output/output.json"

    if len(sys.argv) < 2:
        # No arguments provided - use defaults
        input_file = default_input
        output_file = default_output
        logger.info("No arguments provided, using defaults: %s -> %s", input_file, output_file)
    elif len(sys.argv) == 2:
        # Only input file provided
        input_file = sys.argv[1]
        output_file = default_output
    else:
        # Both input and output provided
        input_file = sys.argv[1]
        output_file = sys.argv[2]
    
    try:
        logger.info("Loading input file: %s", input_file)
        transcript = load_transcript(input_file)
        logger.info("Input loaded (%d characters). Starting LangGraph extraction.", len(transcript))
        
        # Extract actions
        actions = extract_actions(transcript)
        
        # Write output
        logger.info("Writing output to: %s", output_file)
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(actions, f, indent=2, ensure_ascii=False)
        
        logger.info("Done. Extracted %d action(s).", len(actions))
        logger.info("Results saved to %s", output_file)
        logger.info("Log saved to %s", LOG_FILE)

    except FileNotFoundError:
        logger.error("Input file not found: %s", input_file)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in input file: %s", e)
        sys.exit(1)
    except ValueError as e:
        logger.error("Invalid input format: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
