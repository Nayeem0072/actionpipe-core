#!/usr/bin/env python3
"""
Test script: run the same context-resolver-style LLM call multiple times sequentially
and report the time taken for each call. Uses the same prompt and LLM config as the
context resolver node.
"""
import os
import sys
import time

# Ensure project root is on path so we can import from src
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)

from dotenv import load_dotenv
# Load .env from project root so config gets correct URL/model regardless of cwd
load_dotenv(os.path.join(_project_root, ".env"))

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel as PydanticBaseModel, field_validator

from src.action_extractor.nodes import create_context_resolver_llm
from src.action_extractor.llm_config import CONTEXT_RESOLVER_CONFIG


# Same structure as in action_extractor/nodes._context_resolver_llm_call
class ResolutionResult(PydanticBaseModel):
    resolved_segments: list[dict]
    new_actions: list[dict]
    updated_actions: list[dict]
    still_unresolved: list[dict]

    @field_validator("still_unresolved", mode="before")
    @classmethod
    def still_unresolved_to_dicts(cls, v: list) -> list:
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            if isinstance(item, str):
                out.append({"text": item})
            elif isinstance(item, dict):
                out.append(item)
        return out


# Exact prompt from the user (same as the one that was timing out)
SYSTEM_CONTENT = """You are resolving references and linking actions across meeting chunks.

Your tasks:
1. Complete references: If a segment says "I'll do that", link it to the most recent relevant topic
2. Link ownership: Connect vague mentions ("needs fixing") with specific commitments ("I'll handle")
3. Link deadlines: If a later segment mentions a deadline, attach it to earlier related actions
4. Track topics: Maintain active topic memory

For each new segment, determine:
- Does it complete a previous unresolved reference? (provide the link)
- Does it create a new action that should be merged with existing ones?
- Does it add deadline/assignee info to existing actions?

Return JSON with:
{
  "resolved_segments": [...],
  "new_actions": [...],
  "updated_actions": [...],
  "still_unresolved": [...]
}"""

HUMAN_CONTENT = """Context:

Active topics:
- yeah summary only: {'speaker': 'John', 'chunk': 4, 'resolved': False}
- im noting that: {'speaker': 'Priya', 'chunk': 4, 'resolved': True}
- good catch phase one basic roles only admin and us: {'speaker': 'John', 'chunk': 4, 'resolved': False}


New segments from current chunk:
Priya: timeline wise backend how long mike [question]
Mike: if no more surprise fires maybe 10 days dev plus testing time which is theoretical [information]
Sara: frontend i need a week after api stabilizes [information]
John: ok so roughly end of month internal [decision]
Priya: client wont that [information]
John: yeah but reality [information]
Sara: should we send them an update email today [suggestion]
John: yes good point we need to reset expectations [decision]

Previous actions:
0. Note to circle back to flaky tests later (assignee: Priya, deadline: later)
1. Note down the agreed MVP scope details including auth dashboard, basic summary reports, and exclusion of custom exports and full analytics. (assignee: Priya, deadline: None)"""


def main():
    num_calls = 3  # run same call 3 times; change if you want more
    timeout_sec = CONTEXT_RESOLVER_CONFIG.get("timeout", 120)

    print(f"Context resolver config: timeout={timeout_sec}s")
    print(f"  API URL: {CONTEXT_RESOLVER_CONFIG.get('api_url')}")
    print(f"  Model:   {CONTEXT_RESOLVER_CONFIG.get('model_name')}")
    if timeout_sec < 180:
        print("Tip: If calls time out, set CONTEXT_RESOLVER_TIMEOUT=180 (or 240) in .env")
    print(f"Making {num_calls} identical LLM calls (same prompt), one by one...")
    print(
        "Note: In the UI you may see a fast reply because of streaming (tokens as they arrive). "
        "Here we wait for the full JSON response, so total time can be longer.\n"
    )

    llm = create_context_resolver_llm()
    structured_llm = llm.with_structured_output(ResolutionResult, method="json_mode")

    messages = [
        SystemMessage(content=SYSTEM_CONTENT),
        HumanMessage(content=HUMAN_CONTENT),
    ]

    times_sec = []
    for i in range(num_calls):
        print(f"Call {i + 1}/{num_calls} ... ", end="", flush=True)
        start = time.perf_counter()
        try:
            result = structured_llm.invoke(messages)
            elapsed = time.perf_counter() - start
            times_sec.append(elapsed)
            print(f"done in {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.perf_counter() - start
            print(f"failed after {elapsed:.2f}s: {e}")
            raise

    if times_sec:
        print()
        print("Summary:")
        print(f"  Min: {min(times_sec):.2f}s")
        print(f"  Max: {max(times_sec):.2f}s")
        print(f"  Avg: {sum(times_sec) / len(times_sec):.2f}s")
        print(f"  Timeout config: {timeout_sec}s")


if __name__ == "__main__":
    main()
