import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path

import git
import httpx
from dotenv import load_dotenv

load_dotenv()

REPOS_DIR = os.path.expanduser(os.getenv("REPOS_DIR", "./repos"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")

MAX_OUTPUT_CHARS = 8000
MAX_FILE_LINES = 500


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:3000]
    tail = text[-2000:]
    omitted = len(text) - 5000
    return f"{head}\n...[{omitted} chars truncated]...\n{tail}"


def _inject_token_into_url(url: str) -> str:
    """Inject GitHub token into HTTPS clone URLs for auth."""
    if GITHUB_TOKEN and url.startswith("https://github.com"):
        return url.replace("https://github.com", f"https://{GITHUB_TOKEN}@github.com")
    return url


def _extract_owner_repo(remote_url: str) -> tuple[str, str]:
    """Parse owner/repo from a GitHub remote URL."""
    # Handle HTTPS: https://github.com/owner/repo.git
    # Handle SSH:   git@github.com:owner/repo.git
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub owner/repo from URL: {remote_url}")
    return match.group(1), match.group(2)


# --- Tool implementations ---

async def _run_shell(command: str, cwd: str = None, timeout: int = 60) -> str:
    timeout = min(int(timeout), 300)
    working_dir = cwd or REPOS_DIR
    working_dir = os.path.expanduser(working_dir)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {timeout}s"

        output = stdout.decode("utf-8", errors="replace")
        result = f"exit_code={proc.returncode}\n{output}"
        return truncate_output(result)
    except Exception as e:
        return f"Error running command: {type(e).__name__}: {e}"


def _read_file(path: str) -> str:
    path = os.path.expanduser(path)
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        shown = lines[:MAX_FILE_LINES]
        header = f"File: {path} ({total} total lines, showing first {min(total, MAX_FILE_LINES)})\n"
        return header + "".join(shown)
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except Exception as e:
        return f"Error reading file: {type(e).__name__}: {e}"


def _write_file(path: str, content: str) -> str:
    path = os.path.expanduser(path)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {type(e).__name__}: {e}"


def _list_files(path: str, pattern: str = "**/*") -> str:
    path = os.path.expanduser(path)
    try:
        p = Path(path)
        entries = sorted(p.rglob(pattern if pattern != "**/*" else "*"))
        # Filter to files only, skip hidden dirs like .git
        files = [
            str(e.relative_to(p))
            for e in entries
            if e.is_file() and ".git" not in e.parts
        ]
        if len(files) > 500:
            files = files[:500]
            files.append(f"...[truncated, showing first 500 of {len(entries)} entries]")
        return "\n".join(files) if files else "(empty directory)"
    except Exception as e:
        return f"Error listing files: {type(e).__name__}: {e}"


def _search_code(path: str, pattern: str, file_glob: str = None) -> str:
    path = os.path.expanduser(path)
    try:
        # Prefer rg (ripgrep) if available
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading"]
            if file_glob:
                cmd += ["--glob", file_glob]
            cmd += [pattern, path]
        else:
            cmd = ["grep", "-rn"]
            if file_glob:
                cmd += [f"--include={file_glob}"]
            cmd += [pattern, path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout
        lines = output.splitlines()
        if len(lines) > 200:
            lines = lines[:200]
            lines.append(f"...[truncated, showing first 200 matches]")
        return "\n".join(lines) if lines else "No matches found."
    except subprocess.TimeoutExpired:
        return "Error: search timed out after 30s"
    except Exception as e:
        return f"Error searching code: {type(e).__name__}: {e}"


async def _clone_repo(repo_url: str, dest_name: str = None) -> str:
    authed_url = _inject_token_into_url(repo_url)
    name = dest_name or repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = os.path.join(REPOS_DIR, name)

    if os.path.exists(dest):
        return f"Directory already exists: {dest}. Use run_shell to pull updates if needed."

    os.makedirs(REPOS_DIR, exist_ok=True)
    try:
        await asyncio.to_thread(git.Repo.clone_from, authed_url, dest)
        return f"Cloned to {dest}"
    except git.GitCommandError as e:
        return f"Git clone failed: {e}"


def _git_commit_push(repo_path: str, message: str, branch: str = None) -> str:
    repo_path = os.path.expanduser(repo_path)
    try:
        repo = git.Repo(repo_path)
        repo.git.add("-A")
        if not repo.index.diff("HEAD") and not repo.untracked_files:
            return "Nothing to commit (working tree clean)."
        commit = repo.index.commit(message)
        target_branch = branch or repo.active_branch.name
        origin = repo.remote("origin")
        origin.push(refspec=f"{target_branch}:{target_branch}")
        return f"Committed {commit.hexsha[:8]} and pushed to {target_branch}."
    except git.GitCommandError as e:
        return f"Git error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def _create_pull_request(
    repo_path: str,
    title: str,
    body: str,
    base_branch: str = "main",
    head_branch: str = None,
) -> str:
    repo_path = os.path.expanduser(repo_path)
    try:
        repo = git.Repo(repo_path)
        remote_url = repo.remotes.origin.url
        owner, repo_name = _extract_owner_repo(remote_url)
        head = head_branch or repo.active_branch.name

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                json={"title": title, "body": body, "head": head, "base": base_branch},
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            pr = resp.json()
            return f"PR created: {pr['html_url']}"
    except httpx.HTTPStatusError as e:
        return f"GitHub API error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error creating PR: {type(e).__name__}: {e}"


def _git_status(repo_path: str) -> str:
    repo_path = os.path.expanduser(repo_path)
    try:
        repo = git.Repo(repo_path)
        branch = repo.active_branch.name
        status = repo.git.status("--short")
        return f"Branch: {branch}\n{status if status else '(clean)'}"
    except Exception as e:
        return f"Error getting git status: {type(e).__name__}: {e}"


def _list_repos() -> str:
    try:
        if not os.path.exists(REPOS_DIR):
            return f"REPOS_DIR ({REPOS_DIR}) does not exist yet. Clone a repo first."
        results = []
        for name in sorted(os.listdir(REPOS_DIR)):
            full_path = os.path.join(REPOS_DIR, name)
            if not os.path.isdir(full_path):
                continue
            try:
                repo = git.Repo(full_path)
                branch = repo.active_branch.name
                results.append(f"{name}  [{branch}]  {full_path}")
            except git.InvalidGitRepositoryError:
                results.append(f"{name}  [not a git repo]  {full_path}")
        return "\n".join(results) if results else f"No repos found in {REPOS_DIR}"
    except Exception as e:
        return f"Error listing repos: {type(e).__name__}: {e}"


# --- Dispatch ---

TOOL_MAP = {
    "run_shell": _run_shell,
    "read_file": _read_file,
    "write_file": _write_file,
    "list_files": _list_files,
    "search_code": _search_code,
    "clone_repo": _clone_repo,
    "git_commit_push": _git_commit_push,
    "create_pull_request": _create_pull_request,
    "git_status": _git_status,
    "list_repos": _list_repos,
}


async def execute_tool(name: str, inputs: dict) -> str:
    try:
        fn = TOOL_MAP.get(name)
        if fn is None:
            return f"Error: Unknown tool '{name}'"
        if asyncio.iscoroutinefunction(fn):
            return await fn(**inputs)
        else:
            return await asyncio.to_thread(fn, **inputs)
    except TypeError as e:
        return f"Tool called with wrong arguments ({name}): {e}"
    except Exception as e:
        return f"Tool error ({name}): {type(e).__name__}: {e}"


# --- Tool definitions for Claude ---

TOOL_DEFINITIONS = [
    {
        "name": "run_shell",
        "description": (
            "Run any shell command on the local Mac. Use for git operations, running tests, "
            "installing packages, building projects, npm/yarn, python scripts, etc. "
            "Commands run in REPOS_DIR by default unless cwd is specified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (absolute path). Defaults to REPOS_DIR.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60, max 300).",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file (up to 500 lines).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, creating or overwriting it. Parent directories are created automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a directory. Optionally filter by glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory"},
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern e.g. '**/*.py'. Default: all files.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a regex pattern in files under a directory. Returns file:line:match results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to search under"},
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "file_glob": {
                    "type": "string",
                    "description": "File filter glob e.g. '*.py'. Default: all files.",
                },
            },
            "required": ["path", "pattern"],
        },
    },
    {
        "name": "clone_repo",
        "description": "Clone a GitHub repository into REPOS_DIR.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "HTTPS or SSH URL of the repo (e.g. https://github.com/owner/repo)",
                },
                "dest_name": {
                    "type": "string",
                    "description": "Directory name for the clone. Defaults to repo name from URL.",
                },
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "git_commit_push",
        "description": "Stage all changes (git add -A), commit, and push to the remote.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the git repo",
                },
                "message": {"type": "string", "description": "Commit message"},
                "branch": {
                    "type": "string",
                    "description": "Branch to push to. Defaults to current branch.",
                },
            },
            "required": ["repo_path", "message"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a GitHub pull request from the current branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the git repo",
                },
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description (markdown supported)"},
                "base_branch": {
                    "type": "string",
                    "description": "Target branch to merge into. Default: main.",
                },
                "head_branch": {
                    "type": "string",
                    "description": "Source branch with changes. Defaults to current branch.",
                },
            },
            "required": ["repo_path", "title", "body"],
        },
    },
    {
        "name": "git_status",
        "description": "Get the git status and current branch of a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the git repo",
                }
            },
            "required": ["repo_path"],
        },
    },
    {
        "name": "list_repos",
        "description": "List all cloned repositories in REPOS_DIR with their current branch.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]
