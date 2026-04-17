import os

import pytest

from bot import prompts


def test_validate_filename_ok():
    assert prompts.validate_filename("my_prompt.md") == "my_prompt.md"
    assert prompts.validate_filename("Bug Fix.txt") == "Bug Fix.txt"


def test_validate_filename_rejects_bad_extension():
    with pytest.raises(ValueError, match="Поддерживаются"):
        prompts.validate_filename("evil.exe")


def test_validate_filename_strips_path_component():
    # Path components are stripped — only basename is kept
    assert prompts.validate_filename("../secret.md") == "secret.md"
    assert prompts.validate_filename("sub/dir/name.txt") == "name.txt"


def test_validate_filename_rejects_unsafe_chars():
    with pytest.raises(ValueError, match="Недопустимые"):
        prompts.validate_filename("bad@name.md")


def test_validate_filename_length_limit():
    long_name = "a" * 60 + ".md"
    with pytest.raises(ValueError, match="длиннее"):
        prompts.validate_filename(long_name)


def test_save_and_read_prompt(tmp_path):
    d = str(tmp_path / "prompts")
    path = prompts.save_prompt(d, "hello.md", b"# Hello\nworld")
    assert path.exists()
    assert prompts.read_prompt(d, "hello.md") == "# Hello\nworld"


def test_save_prompt_rejects_oversized(tmp_path):
    d = str(tmp_path / "prompts")
    content = b"x" * (prompts.MAX_FILE_SIZE + 1)
    with pytest.raises(ValueError, match="больше"):
        prompts.save_prompt(d, "big.md", content)


def test_list_prompts_sorted(tmp_path):
    d = str(tmp_path / "prompts")
    prompts.save_prompt(d, "b.md", b"b")
    prompts.save_prompt(d, "a.txt", b"a")
    prompts.save_prompt(d, "c.md", b"c")
    assert prompts.list_prompts(d) == ["a.txt", "b.md", "c.md"]


def test_list_prompts_ignores_other_extensions(tmp_path):
    d = str(tmp_path / "prompts")
    os.makedirs(d, exist_ok=True)
    (tmp_path / "prompts" / "note.md").write_text("ok")
    (tmp_path / "prompts" / "junk.bin").write_text("bin")
    assert prompts.list_prompts(d) == ["note.md"]


def test_delete_prompt(tmp_path):
    d = str(tmp_path / "prompts")
    prompts.save_prompt(d, "temp.md", b"tmp")
    prompts.delete_prompt(d, "temp.md")
    assert prompts.list_prompts(d) == []


def test_delete_missing_raises(tmp_path):
    d = str(tmp_path / "prompts")
    with pytest.raises(ValueError, match="не найден"):
        prompts.delete_prompt(d, "nonexistent.md")


def test_read_missing_raises(tmp_path):
    d = str(tmp_path / "prompts")
    with pytest.raises(ValueError, match="не найден"):
        prompts.read_prompt(d, "nonexistent.md")
