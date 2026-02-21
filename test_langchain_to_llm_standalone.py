#!/usr/bin/env python3
"""
Standalone test: same prompt, multiple sequential LLM calls. No .env, no src imports.
Edit the CONFIG block below to set API URL, model, and timeout.
"""
import time
import httpx
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel as PydanticBaseModel, field_validator

# -----------------------------------------------------------------------------
# CONFIG — change these; nothing is read from .env or the project
# -----------------------------------------------------------------------------
API_BASE_URL = "http://localhost:11434/v1"   # e.g. Ollama OpenAI-compatible endpoint
API_KEY = "ollama"                      # often unused for local
MODEL_NAME = "glm-4.7-flash"                       # or your model name
TIMEOUT_SEC = 300                           # seconds to wait for full response (e.g. 300 = 5 min)
MAX_RETRIES = 0                             # 0 = fail once on timeout; no retries
NUM_CALLS = 3                               # how many identical calls to run
# -----------------------------------------------------------------------------


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
    print(f"Config: {API_BASE_URL}  model={MODEL_NAME}  timeout={TIMEOUT_SEC}s  max_retries={MAX_RETRIES}")
    print(f"Making {NUM_CALLS} identical LLM calls (same prompt), one by one...\n")

    llm = ChatOpenAI(
        base_url=API_BASE_URL,
        api_key=API_KEY,
        model=MODEL_NAME,
        temperature=0.3,
        max_tokens=2000,
        timeout=httpx.Timeout(TIMEOUT_SEC),
        max_retries=MAX_RETRIES,
    )
    structured_llm = llm.with_structured_output(ResolutionResult, method="json_mode")

    messages = [
        SystemMessage(content=SYSTEM_CONTENT),
        HumanMessage(content=HUMAN_CONTENT),
    ]

    times_sec = []
    for i in range(NUM_CALLS):
        print(f"Call {i + 1}/{NUM_CALLS} ... ", end="", flush=True)
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


if __name__ == "__main__":
    main()
