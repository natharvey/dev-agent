import json
import os

import anthropic
from dotenv import load_dotenv

from tools import TOOL_DEFINITIONS, execute_tool

load_dotenv()

REPOS_DIR = os.path.expanduser(os.getenv("REPOS_DIR", "./repos"))
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "sessions.json")

client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = f"""You are Dev, a personal software engineering agent that operates via WhatsApp.
You have direct access to your operator's Mac filesystem and can run shell commands, read/write files, search code, clone repos, and create commits and PRs on GitHub.

REPOS_DIR is {REPOS_DIR} — this is where all cloned repositories live. Always use absolute paths derived from REPOS_DIR when working with repos.

Guidelines:
- Be concise. WhatsApp is a mobile chat interface. Avoid markdown headers or walls of text. Short paragraphs and brief bullet points only.
- Work autonomously. Never ask for confirmation or permission — just do it.
- For multi-step tasks, briefly narrate progress in one line ("Cloning... done. Running tests...") so the user knows you're working.
- Always include the URL or commit SHA when you create a PR or push a commit.
- If a tool returns an error, diagnose and attempt to recover before asking the user for help.
- Use list_files and search_code to orient yourself in unfamiliar repos before making changes.
- Keep responses short enough to read comfortably on a phone screen."""

def _serialize_content(content) -> list | str:
    """Convert Anthropic SDK content blocks to plain JSON-serializable dicts."""
    if isinstance(content, str):
        return content
    result = []
    for block in content:
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, "type"):
            if block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif block.type == "tool_result":
                result.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content})
            elif block.type == "thinking":
                result.append({"type": "thinking", "thinking": block.thinking})
    return result


def _serialize_history(history: list) -> list:
    return [{"role": msg["role"], "content": _serialize_content(msg["content"])} for msg in history]


def _load_sessions() -> dict[str, list]:
    try:
        with open(SESSIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(sessions: dict) -> None:
    try:
        serializable = {k: _serialize_history(v) for k, v in sessions.items()}
        with open(SESSIONS_FILE, "w") as f:
            json.dump(serializable, f)
    except Exception:
        pass  # never crash the agent over a save failure


# In-memory conversation history keyed by phone number
sessions: dict[str, list] = _load_sessions()

# Track which users are currently being processed
processing: set[str] = set()

MAX_HISTORY_MESSAGES = 40  # ~20 turns
SUMMARISE_THRESHOLD = 12  # summarise when history exceeds this many messages
KEEP_RECENT = 6  # always keep last N messages verbatim


def _is_user_text_message(msg: dict) -> bool:
    """True if this is a regular user text message (not a tool_result block)."""
    if msg["role"] != "user":
        return False
    content = msg["content"]
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _prune_history(history: list) -> list:
    """Remove oldest complete turn, never splitting tool_use/tool_result pairs."""
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    # Find the second user text message — everything before it is safe to drop
    for i in range(1, len(history)):
        if _is_user_text_message(history[i]):
            return history[i:]
    return history


def _history_to_text(history: list) -> str:
    """Convert history to readable text for summarisation."""
    parts = []
    for msg in history:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            parts.append(f"{role}: {content[:500]}")
        elif isinstance(content, list):
            for block in content:
                if hasattr(block, "text") and block.text:
                    parts.append(f"{role}: {block.text[:500]}")
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(f"{role}: {block['text'][:500]}")
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool call: {block['name']}]")
                    elif block.get("type") == "tool_result":
                        parts.append(f"[tool result: {str(block.get('content', ''))[:200]}]")
    return "\n".join(parts)


async def _maybe_summarise(history: list) -> list:
    """Compress old history into a summary when it gets too long."""
    if len(history) <= SUMMARISE_THRESHOLD:
        return history

    # Find a safe split point: last user text message before the recent window
    split_at = None
    for i in range(len(history) - KEEP_RECENT - 1, 0, -1):
        if _is_user_text_message(history[i]):
            split_at = i
            break

    if not split_at or split_at < 2:
        return history

    to_summarise = history[:split_at]
    recent = history[split_at:]

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    "Summarise this dev agent conversation concisely. "
                    "Include: tasks completed, files changed, repos cloned, "
                    "commit SHAs, PR URLs, errors encountered, and current state. Be specific.\n\n"
                    + _history_to_text(to_summarise)
                )
            }]
        )
        summary = resp.content[0].text
    except Exception:
        return history  # if summarisation fails, keep history as-is

    return [
        {"role": "user", "content": f"[Earlier conversation summary: {summary}]"},
        {"role": "assistant", "content": [{"type": "text", "text": "Understood, I have context from our earlier work."}]},
    ] + recent


def reset_session(from_number: str) -> None:
    sessions.pop(from_number, None)
    _save_sessions(sessions)


def is_processing(from_number: str) -> bool:
    return from_number in processing


async def process_message(from_number: str, user_text: str) -> str:
    processing.add(from_number)
    try:
        return await _run_agent_loop(from_number, user_text)
    finally:
        processing.discard(from_number)


async def _run_agent_loop(from_number: str, user_text: str) -> str:
    history = sessions.setdefault(from_number, [])
    history.append({"role": "user", "content": user_text})
    history = await _maybe_summarise(history)
    sessions[from_number] = _prune_history(history)
    history = sessions[from_number]
    _save_sessions(sessions)

    try:
        while True:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOL_DEFINITIONS,
                messages=history,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )

            # Always append full response.content to preserve tool_use blocks
            history.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text = next(
                    (b.text for b in response.content if hasattr(b, "text")), "Done."
                )
                return text

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await execute_tool(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )
                history.append({"role": "user", "content": tool_results})
                _save_sessions(sessions)
                # Loop back — Claude will see the results and continue

            elif response.stop_reason == "max_tokens":
                return "Hit token limit mid-task. Try asking for a smaller chunk of work."

            else:
                return f"Unexpected stop reason: {response.stop_reason}. Try again."

    except anthropic.APIStatusError as e:
        return f"API error {e.status_code}: {e.message}"
    except Exception as e:
        return f"Something went wrong: {type(e).__name__}: {e}"
