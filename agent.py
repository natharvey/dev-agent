import json
import os

import anthropic
from dotenv import load_dotenv

from tools import TOOL_DEFINITIONS, execute_tool

load_dotenv()

REPOS_DIR = os.path.expanduser(os.getenv("REPOS_DIR", "./repos"))

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

# In-memory conversation history keyed by phone number
sessions: dict[str, list] = {}

# Track which users are currently being processed
processing: set[str] = set()

MAX_HISTORY_MESSAGES = 40  # ~20 turns


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


def reset_session(from_number: str) -> None:
    sessions.pop(from_number, None)


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
    sessions[from_number] = _prune_history(history)
    history = sessions[from_number]

    try:
        while True:
            response = await client.messages.create(
                model="claude-opus-4-6",
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=history,
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
                # Loop back — Claude will see the results and continue

            elif response.stop_reason == "max_tokens":
                return "Hit token limit mid-task. Try asking for a smaller chunk of work."

            else:
                return f"Unexpected stop reason: {response.stop_reason}. Try again."

    except anthropic.APIStatusError as e:
        return f"API error {e.status_code}: {e.message}"
    except Exception as e:
        return f"Something went wrong: {type(e).__name__}: {e}"
