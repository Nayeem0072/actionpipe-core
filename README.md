# LangGraph Meeting Action Item Extractor

A LangGraph-based agent that processes raw meeting transcripts and extracts structured action items. Each node in the pipeline runs locally via Ollama's OpenAI-compatible API, so no data leaves your machine.

---

## Features

- Segments transcripts into conversational chunks (no AI needed at this step)
- Filters out irrelevant chunks (small talk, audio glitches, etc.) before spending LLM time
- Extracts action items, decisions, and suggestions from each relevant chunk
- Cleans ASR noise (um, uh, filler words) and deduplicates within chunks
- Resolves cross-chunk references — links "I'll do that" to the actual task from a prior chunk
- Deduplicates actions across the full transcript and produces a final, chronologically sorted list

---

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure your model is running via Ollama:
```bash
ollama run glm-4.7-flash
```

---

## Configuration

Create a `.env` file in the project root:

```env
GLM_API_URL=http://localhost:11434/v1
GLM_API_KEY=ollama
MODEL_NAME=glm-4.7-flash
TEMPERATURE=0
MAX_TOKENS=2500
TOP_P=1
REPEAT_PENALTY=1.1
PRESENCE_PENALTY=0.0
```

Per-node overrides (optional — fall back to the above if not set):

```env
CONTEXT_RESOLVER_TIMEOUT=180
CONTEXT_RESOLVER_MAX_PREVIOUS_ACTIONS=5
CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM=8
```

---

## Usage

Run with default files (`input.txt` → `output.json`):

```bash
python run_langgraph.py
```

Run with a custom input file (output still goes to `output.json`):

```bash
python run_langgraph.py my_transcript.txt
```

Run with both custom input and output files:

```bash
python run_langgraph.py my_transcript.txt my_output.json
```

Or using the module directly:

```bash
python -m src.langgraph_main [input_file] [output_file]
```

Or call from Python:

```python
from src.langgraph_workflow import extract_actions

actions = extract_actions(transcript_raw="<your transcript text>")
```

---

## Workflow

The pipeline is a directed graph built with LangGraph. Each node is timed and logged. After the initial segmentation, the graph loops chunk-by-chunk until all chunks are processed, then runs the final deduplication and finalisation steps.

```
                    ┌─────────────┐
                    │  Segmenter  │  (no LLM)
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
              ┌────►│RelevanceGate│◄───────────────────────────────────────────┐
              │     └──────┬──────┘                                             │
              │            │                                                     │
              │    YES ────┘──── NO                                             │
              │    │              │                                              │
              │    ▼              ▼                                              │
              │ ┌──────────┐  ┌────────────────┐                                │
              │ │  Local   │  │ increment_chunk ├────────────────────────────────┘
              │ │Extractor │  └────────────────┘
              │ └────┬─────┘
              │      │
              │ ┌────▼──────────────┐
              │ │EvidenceNormalizer │  (no LLM)
              │ └────┬──────────────┘
              │      │
              │ ┌────▼──────────────┐
              │ │  ContextResolver  │
              │ └────┬──────────────┘
              │      │
              └──────┘ (via increment_chunk)

After all chunks:

┌──────────────────────┐      ┌─────────────────┐
│  GlobalDeduplicator  │ ───► │ ActionFinalizer  │ ───► END
└──────────────────────┘      └─────────────────┘
```

---

## Node Details

### 1. Segmenter *(no LLM)*
Splits the raw transcript into speaker turns using a regex pattern (`Speaker: text`). Turns are grouped into chunks of 8 speaker turns each to keep LLM prompts small and respect context window limits. No AI is involved — this is pure structural chunking.

**Output:** a list of chunks, each containing up to 8 speaker turns.

---

### 2. Relevance Gate *(LLM)*
For each chunk, asks the LLM a single binary question: *"Does this chunk contain work-relevant operational content?"*

**Relevant:** tasks, assignments, decisions, timelines, technical/business discussion, ownership, deadlines.  
**Not relevant:** greetings, small talk, jokes, audio glitches, off-topic conversation.

If the answer is **YES**, the chunk proceeds to extraction. If **NO**, the chunk is skipped and the next one is loaded. If all chunks are exhausted, the loop ends and the graph moves to deduplication.

This gate avoids wasting LLM calls on filler content.

---

### 3. Local Extractor *(LLM)*
Extracts structured segments from the current chunk. For each relevant utterance, it identifies:

- **speaker** — who said it
- **text** — the exact utterance from the transcript
- **intent** — one of: `action_item`, `decision`, `suggestion`, `information`, `question`, `agreement`, `clarification`
- **resolved_context** — what earlier topic this utterance refers to (if any)
- **context_unclear** — whether the reference cannot be resolved from the chunk
- **action_details** *(for `action_item` only)* — a fully self-contained description, assignee, deadline, and confidence score

A key requirement for `action_details.description` is that it must be self-contained: pronouns like "it", "that", "this" must be resolved using surrounding context. The LLM is instructed to write descriptions that are understandable without reading the transcript.

---

### 4. Evidence Normalizer *(no LLM)*
Cleans the extracted segments without any additional LLM calls:

- **ASR noise removal** — strips filler words (`um`, `uh`, `er`, `ah`, `like`, `you know`)
- **Whitespace normalisation** — collapses multiple spaces
- **Intra-chunk deduplication** — drops exact duplicate utterances within the same chunk
- **Verb normalisation** — maps informal verbs to canonical forms (e.g. `"take care of"` → `"fix"`, `"gonna"` → `"will"`)

---

### 5. Context Resolver *(LLM)*
The core cross-chunk reasoning step. Given the segments from the current chunk plus memory of prior chunks (active topics, unresolved references, previous actions), it:

- **Completes references** — links fragments like "I'll do that" to the specific task from a prior chunk
- **Links ownership** — connects vague mentions ("needs fixing") with explicit commitments ("I'll handle it")
- **Links deadlines** — attaches a deadline mentioned later to an earlier related action
- **Tracks topics** — maintains a rolling memory of active topics across chunks

To prevent timeouts on local models, chunks with more than `CONTEXT_RESOLVER_MAX_SEGMENTS_FOR_LLM` (default: 8) segments skip the LLM and fall back to using the action_item segments directly without cross-chunk linking.

---

### 6. Global Deduplicator *(no LLM)*
After all chunks are processed, merges actions that refer to the same real-world task. Two actions are considered duplicates if:

- **Same speaker**
- **Similar verb** (using a synonym map: e.g. `fix`, `handle`, `deal with` are treated as equivalent)
- **High word overlap in description** (≥ 30% word overlap)
- **Close meeting window** (within 3 chunks of each other)

When duplicates are merged, the best available assignee and deadline are kept, and confidence is taken as the maximum across the group.

---

### 7. Action Finalizer *(no LLM)*
Enforces the output schema and drops low-quality results:

- Skips actions with no description
- Drops actions with confidence below 0.3 (likely hallucinations)
- Defaults `assignee` to the speaker if no assignee was extracted
- Normalises verbs to canonical forms
- Deduplicates `source_spans` within each action
- Sorts the final list chronologically by the chunk in which the action first appeared

---

## Output

A JSON array saved to `output.json`. Each action item contains:

| Field | Description |
|-------|-------------|
| `description` | Full, self-contained description of what needs to be done |
| `assignee` | Person responsible (defaults to speaker if not specified) |
| `deadline` | When it is due (e.g. `"end of month"`, `"March 15"`) or `null` |
| `speaker` | Who raised the action in the meeting |
| `verb` | Normalised action verb (e.g. `"fix"`, `"send"`, `"review"`) |
| `confidence` | Extraction confidence score (0.0–1.0) |
| `source_spans` | List of segment IDs the action was derived from |
| `meeting_window` | Chunk range `[start, end]` where the action was discussed |

Example:

```json
[
  {
    "description": "Draft email to Client Delta to reset expectations on the March 15 delivery date, including phased delivery plan",
    "assignee": "John",
    "deadline": "today",
    "speaker": "John",
    "verb": "send",
    "confidence": 0.85,
    "source_spans": ["a3f1c2d4e5b6"],
    "meeting_window": [4, 4]
  }
]
```

---

## Project Structure

```
agent-ai/
├── src/
│   ├── __init__.py
│   ├── langgraph_main.py        # Entry point (CLI arg parsing, logging, file I/O)
│   ├── langgraph_workflow.py    # Graph definition and extract_actions()
│   ├── langgraph_nodes.py       # All node implementations
│   ├── langgraph_state.py       # Graph state schema (TypedDict)
│   ├── langgraph_models.py      # Pydantic models (Segment, Action, ActionDetails)
│   └── langgraph_llm_config.py  # Per-node LLM configuration (loaded from .env)
├── tests/
│   ├── test_langchain_to_llm.py            # Timed LLM call test (uses project config)
│   └── test_langchain_to_llm_standalone.py # Timed LLM call test (self-contained config)
├── docs/                        # Reference documents and example files
├── run_langgraph.py             # Convenience runner script
├── input.txt                    # Default input transcript
├── output.json                  # Default output (generated on run)
├── output_log.txt               # Execution log (generated on run)
├── .env                         # API URL, model name, generation params
└── requirements.txt
```

---

## Requirements

- Python 3.10+
- Ollama with `glm-4.7-flash` (or any OpenAI-compatible model served locally)
