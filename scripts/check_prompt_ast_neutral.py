#!/usr/bin/env python3
"""Fail if a prompt rewrite changed anything but string text.

Usage: check_prompt_ast_neutral.py BASE_REF [--head REF] [--only PREFIX ...]

For every .py file that differs between BASE_REF and HEAD, parse the old and new source,
blank every str constant (docstrings and f-string text included) and compare ast.dump of the
two trees. Any difference prints the file and exits 1. A file that is added or deleted counts
as a difference. Comments never reach the AST, so editing them is always neutral.

Limit: a str that is code rather than prose (a dict key, a getattr name) is blanked too, so
renaming one is not caught. Review diffs of those by eye.

It also prints the size of the composed synthesis prompt at BASE and HEAD, in characters and
in token-ish units (characters / 4), when it can be composed statically.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys

COORDINATOR = "src/bifrost/research/coordinator.py"
POLICY = "src/bifrost/research/confidence_policy.py"
DIAGRAMS = "src/bifrost/research/diagram_standards.py"
PROMPT_NAME = "_SYNTHESIS_SYSTEM_PROMPT"
# coordinator.py imports these two blocks and appends them to the prompt.
IMPORTED = {"CODE_IS_TRUTH_POLICY": POLICY, "DIAGRAM_RULES_BLOCK": DIAGRAMS}


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def neutral_dump(source: str, filename: str = "<source>") -> str:
    tree = _BlankStrings().visit(ast.parse(source, filename=filename))
    return ast.dump(tree)


def differs(old: str | None, new: str | None, filename: str = "<source>") -> bool:
    """True when the two sources differ in anything but string text. None means absent."""
    if old is None or new is None:
        return old is not new
    return neutral_dump(old, filename) != neutral_dump(new, filename)


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _show(ref: str, path: str) -> str | None:
    done = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def changed_python_files(base: str, head: str) -> list[str]:
    out = _git("diff", "--name-only", "--no-renames", base, head, "--", "*.py")
    return [line for line in out.splitlines() if line]


def _eval_str(node: ast.expr, env: dict[str, str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_str(node.left, env) + _eval_str(node.right, env)
    raise ValueError(f"cannot statically evaluate {ast.dump(node)[:60]}")


def _module_strings(source: str, env: dict[str, str]) -> dict[str, str]:
    """Module-level NAME = <str expression> assignments, evaluated in order."""
    env = dict(env)
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                env[node.targets[0].id] = _eval_str(node.value, env)
            except ValueError:
                continue
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            try:
                env[node.target.id] = _eval_str(node.value, env)
            except ValueError:
                continue
    return env


def composed_prompt(ref: str) -> str:
    """The final _SYNTHESIS_SYSTEM_PROMPT at ``ref``, built from the three source files."""
    sources = {path: _show(ref, path) for path in (COORDINATOR, POLICY, DIAGRAMS)}
    missing = [p for p, s in sources.items() if s is None]
    if missing:
        raise ValueError(f"{ref}: missing {', '.join(missing)}")
    env: dict[str, str] = {}
    for name, path in IMPORTED.items():
        env[name] = _module_strings(sources[path], {})[name]
    final = _module_strings(sources[COORDINATOR], env).get(PROMPT_NAME)
    if final is None:
        raise ValueError(f"{ref}: {PROMPT_NAME} is not a static string expression")
    return final


def _report_size(base: str, head: str) -> None:
    sizes: dict[str, int | None] = {}
    for label, ref in (("BASE", base), ("HEAD", head)):
        try:
            sizes[label] = len(composed_prompt(ref))
        except ValueError as exc:
            sizes[label] = None
            print(f"{label}: composed synthesis prompt not measured ({exc})")
    if sizes["BASE"] is not None and sizes["HEAD"] is not None:
        b, h = sizes["BASE"], sizes["HEAD"]
        print(
            f"composed synthesis prompt: BASE {b} chars (~{b // 4} tokens), "
            f"HEAD {h} chars (~{h // 4} tokens), delta {h - b:+d} chars (~{(h - b) // 4:+d} tokens)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_ref")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PREFIX",
        help="restrict to changed files under this path prefix (repeatable)",
    )
    args = parser.parse_args(argv)

    files = changed_python_files(args.base_ref, args.head)
    if args.only:
        files = [f for f in files if any(f.startswith(p) for p in args.only)]

    bad: list[str] = []
    for path in files:
        old = _show(args.base_ref, path)
        new = _show(args.head, path)
        try:
            if differs(old, new, path):
                bad.append(
                    path
                    if old is not None and new is not None
                    else f"{path} ({'added' if old is None else 'deleted'})"
                )
        except SyntaxError as exc:
            bad.append(f"{path} (does not parse: {exc})")

    print(f"checked {len(files)} changed .py file(s) between {args.base_ref} and {args.head}")
    _report_size(args.base_ref, args.head)
    if bad:
        print("NOT string-only, review these:", file=sys.stderr)
        for path in bad:
            print(f"  {path}", file=sys.stderr)
        return 1
    print("ok: every change is string text only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
