import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".md", ".txt"}
MAX_FILENAME_LEN = 50
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
_SAFE_NAME_RE = re.compile(r"^[\w\-. ]+$", re.UNICODE)


def _resolve_prompts_dir(prompts_dir: str) -> Path:
    """Return absolute Path to prompts dir, creating it if missing."""
    path = Path(prompts_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_filename(filename: str) -> str:
    """Validate prompt filename. Returns sanitized name. Raises ValueError on invalid."""
    name = Path(filename).name
    if not name:
        raise ValueError("Empty filename")
    if len(name.encode("utf-8")) > MAX_FILENAME_LEN:
        raise ValueError(f"Имя файла длиннее {MAX_FILENAME_LEN} байт")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Поддерживаются только {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError("Недопустимые символы в имени")
    return name


def _resolve_target(prompts_dir: str, filename: str) -> tuple[Path, Path]:
    """Return (base, target) with path-traversal protection."""
    name = validate_filename(filename)
    base = _resolve_prompts_dir(prompts_dir)
    target = (base / name).resolve()
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("Выход за пределы каталога шаблонов")
    return base, target


def list_prompts(prompts_dir: str) -> list[str]:
    """Return sorted list of prompt filenames in the dir."""
    path = _resolve_prompts_dir(prompts_dir)
    names = []
    for p in path.iterdir():
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS:
            names.append(p.name)
    return sorted(names)


def save_prompt(prompts_dir: str, filename: str, content: bytes) -> Path:
    """Save prompt content to file. Returns target path."""
    if len(content) > MAX_FILE_SIZE:
        raise ValueError(f"Файл больше {MAX_FILE_SIZE // 1024} КБ")
    _, target = _resolve_target(prompts_dir, filename)
    target.write_bytes(content)
    return target


def read_prompt(prompts_dir: str, filename: str) -> str:
    """Read prompt content. Raises ValueError on invalid name or missing file."""
    _, target = _resolve_target(prompts_dir, filename)
    if not target.is_file():
        raise ValueError(f"Шаблон '{filename}' не найден")
    return target.read_text(encoding="utf-8")


def delete_prompt(prompts_dir: str, filename: str) -> None:
    """Delete a prompt file. Raises ValueError on invalid name or missing file."""
    _, target = _resolve_target(prompts_dir, filename)
    if not target.is_file():
        raise ValueError(f"Шаблон '{filename}' не найден")
    target.unlink()
