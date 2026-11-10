"""The UI hard-codes no Jira host: links come from the API."""

from __future__ import annotations

from pathlib import Path

UI_SRC = Path(__file__).resolve().parent.parent / "ui" / "src"


def test_no_atlassian_host_anywhere_in_ui_src() -> None:
    offenders = [
        str(p.relative_to(UI_SRC))
        for p in UI_SRC.rglob("*")
        if p.is_file()
        and p.suffix in {".ts", ".tsx", ".css", ".json", ".html"}
        and "atlassian.net" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
