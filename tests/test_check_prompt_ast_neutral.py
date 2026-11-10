"""Unit tests for scripts/check_prompt_ast_neutral.py."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_prompt_ast_neutral.py"
spec = importlib.util.spec_from_file_location("check_prompt_ast_neutral", SCRIPT)
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

OLD = """\
\"\"\"Module doc about recipes.\"\"\"

PROMPT = \"\"\"You suggest recipes by pantry contents.\"\"\"


def build(name):
    \"\"\"Build for cooks.\"\"\"
    return f"hello {name}, welcome to the recipe box" + PROMPT
"""

REWORDED = """\
\"\"\"Module doc about meals.\"\"\"

PROMPT = \"\"\"You suggest dishes by pantry contents, and say more than before.\"\"\"


def build(name):
    \"\"\"Build for chefs.\"\"\"
    return f"hi {name}, welcome to the kitchen" + PROMPT
"""


@pytest.fixture
def two_files(tmp_path):
    old, new = tmp_path / "old.py", tmp_path / "new.py"
    old.write_text(OLD)
    return old, new


def _differs(old: Path, new: Path) -> bool:
    return tool.differs(old.read_text(), new.read_text(), new.name)


def test_string_only_rewrite_is_neutral(two_files):
    old, new = two_files
    new.write_text(REWORDED)
    assert not _differs(old, new)


def test_comment_only_change_is_neutral(two_files):
    old, new = two_files
    new.write_text(OLD + "\n" + "#" + " a trailing note\n")
    assert not _differs(old, new)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.replace("return f", "x = 1\n    return f"),
        lambda s: s.replace("def build(name)", "def build(name, extra=None)"),
        lambda s: s.replace("{name}", "{name.upper()}"),
        lambda s: s.replace("+ PROMPT", "+ PROMPT + PROMPT"),
        lambda s: s.replace("PROMPT = ", "PROMPT = 5 or "),
        lambda s: s.replace('    """Build for cooks."""\n', "").replace(
            '    """Build for chefs."""\n', ""
        ),
    ],
    ids=[
        "new statement",
        "new parameter",
        "changed expression",
        "extra operand",
        "added operator",
        "docstring removed",
    ],
)
def test_code_change_is_flagged(two_files, mutation):
    old, new = two_files
    new.write_text(mutation(REWORDED))
    assert _differs(old, new)


def test_added_and_deleted_files_are_differences():
    assert tool.differs(None, "x = 1\n")
    assert tool.differs("x = 1\n", None)
    assert not tool.differs(None, None)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path, new_policy: str) -> Path:
    repo = tmp_path / "repo"
    research = repo / "src" / "bifrost" / "research"
    research.mkdir(parents=True)
    (research / "confidence_policy.py").write_text('CODE_IS_TRUTH_POLICY = """policy text"""\n')
    (research / "diagram_standards.py").write_text('DIAGRAM_RULES_BLOCK = """diagram text"""\n')
    (research / "coordinator.py").write_text(
        '_SYNTHESIS_SYSTEM_PROMPT = """base prompt"""\n'
        "from bifrost.research.confidence_policy import CODE_IS_TRUTH_POLICY\n"
        "from bifrost.research.diagram_standards import DIAGRAM_RULES_BLOCK\n"
        "_SYNTHESIS_SYSTEM_PROMPT = (_SYNTHESIS_SYSTEM_PROMPT + CODE_IS_TRUTH_POLICY + DIAGRAM_RULES_BLOCK)\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "base")
    (research / "confidence_policy.py").write_text(new_policy)
    _git(repo, "commit", "-q", "-am", "rewrite")
    return repo


def test_cli_passes_on_string_only_change_and_reports_size(tmp_path, monkeypatch, capsys):
    repo = _make_repo(
        tmp_path, 'CODE_IS_TRUTH_POLICY = """a much longer replacement policy text"""\n'
    )
    monkeypatch.chdir(repo)
    assert tool.main(["base"]) == 0
    out = capsys.readouterr().out
    assert "checked 1 changed .py file(s)" in out
    base_len = len("base prompt" + "policy text" + "diagram text")
    head_len = len("base prompt" + "a much longer replacement policy text" + "diagram text")
    assert f"BASE {base_len} chars (~{base_len // 4} tokens)" in out
    assert f"HEAD {head_len} chars (~{head_len // 4} tokens)" in out


def test_cli_fails_and_names_the_file_on_code_change(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path, 'CODE_IS_TRUTH_POLICY = """policy text"""\nEXTRA = 1\n')
    monkeypatch.chdir(repo)
    assert tool.main(["base"]) == 1
    assert "src/bifrost/research/confidence_policy.py" in capsys.readouterr().err


def test_cli_reports_unmeasurable_prompt_and_flags_the_deleted_file(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path, 'CODE_IS_TRUTH_POLICY = """changed"""\n')
    _git(repo, "rm", "-q", "src/bifrost/research/diagram_standards.py")
    _git(repo, "commit", "-q", "-m", "drop diagrams")
    monkeypatch.chdir(repo)
    assert tool.main(["base"]) == 1
    out = capsys.readouterr()
    assert "not measured" in out.out
    assert "diagram_standards.py (deleted)" in out.err


def test_only_filter_limits_the_check(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, 'CODE_IS_TRUTH_POLICY = """policy text"""\nEXTRA = 1\n')
    monkeypatch.chdir(repo)
    assert tool.main(["base", "--only", "src/bifrost/other/"]) == 0
