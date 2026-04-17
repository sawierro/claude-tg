import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent


async def _run_git(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command in the repo dir. Returns (returncode, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        "git", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_DIR),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as e:
        process.kill()
        await process.wait()
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from e
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def is_git_repo() -> bool:
    """Check if the project dir is a git repo."""
    code, _, _ = await _run_git("rev-parse", "--git-dir", timeout=5)
    return code == 0


async def current_branch() -> str:
    """Return the current branch name."""
    code, out, _ = await _run_git("rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    return out if code == 0 else ""


async def current_commit() -> str:
    """Return the current commit hash (short)."""
    code, out, _ = await _run_git("rev-parse", "--short", "HEAD", timeout=5)
    return out if code == 0 else ""


async def is_working_tree_dirty() -> bool:
    """Check if there are uncommitted local changes."""
    code, out, _ = await _run_git("status", "--porcelain", timeout=10)
    return code == 0 and bool(out)


async def fetch() -> tuple[bool, str]:
    """Run git fetch. Returns (success, error_message)."""
    code, _, err = await _run_git("fetch", "--quiet", timeout=60)
    if code != 0:
        return False, err or f"git fetch exited {code}"
    return True, ""


async def pending_commits(branch: str = "main", limit: int = 20) -> list[str]:
    """Return list of commit summaries pending between HEAD and origin/<branch>."""
    code, out, _ = await _run_git(
        "log", f"HEAD..origin/{branch}", f"-{limit}", "--pretty=format:%h %s",
        timeout=10,
    )
    if code != 0 or not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


async def pull(branch: str = "main") -> tuple[bool, str]:
    """Run git pull --ff-only. Returns (success, output_or_error)."""
    code, out, err = await _run_git(
        "pull", "--ff-only", "origin", branch, timeout=120
    )
    if code != 0:
        return False, err or out or f"git pull exited {code}"
    return True, out or "Updated."
