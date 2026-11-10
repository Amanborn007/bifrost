"""Pin the STRUCTURE of the synthesis few-shots and their text-to-code couplings.

Wording is free to change (the prompt rewrite replaces the domain nouns). What must not
change: diagram shape, tier order, section headings, and every phrase that other code
matches byte for byte (the evidence-gap needles, the UI header regex, the scorer regexes).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from bifrost.benchmarks import scorer
from bifrost.benchmarks.loader import REPO_ROOT
from bifrost.research import coordinator
from bifrost.research.confidence_policy import CODE_IS_TRUTH_POLICY
from bifrost.research.diagram_standards import DIAGRAM_RULES_BLOCK

SYNTH = coordinator._SYNTHESIS_SYSTEM_PROMPT
SYNTH_BASE = SYNTH.split(CODE_IS_TRUTH_POLICY)[0]
MERMAID_TSX = REPO_ROOT / "ui" / "src" / "components" / "mermaid.tsx"
PROSE_TSX = REPO_ROOT / "ui" / "src" / "components" / "prose.tsx"
COORDINATOR_PY = Path(coordinator.__file__)


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── mermaid example blocks ───────────────────────────────────────────────────

BLOCKS = re.findall(r"```mermaid\n(.*?)```", DIAGRAM_RULES_BLOCK, re.S)
_ARROW_FLOW = re.compile(r"^\s*(\w+)\s*-->(?:\|([^|]*)\|)?\s*(\w+)\s*$")
_NODE_DECL = re.compile(r"^\s*(\w+)\s*(?:\(\[|\[\(|\[|\(|\{)(.*)$")
_NODE_LABEL = re.compile(
    r'^\s*\w+\s*(?:\(\[|\[\(|\[|\(|\{)\s*"?(.*?)"?\s*(?:\]\)|\)\]|\]|\)|\})(?::::\w+)?\s*$'
)
_SEQ_MSG = re.compile(r"^\s*(\w+)\s*(-{1,2}>>)\s*(\w+)\s*:\s*(.*\S)?\s*$")
_STATE_TR = re.compile(r"^\s*(\[\*\]|\w+)\s*-->\s*(\[\*\]|\w+)\s*(?::\s*(.*\S))?\s*$")


def _analyse(block: str) -> dict:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    header = re.match(r"^\s*%%\s*(HLD|LLD)\s*:\s*(.+?)\s*$", lines[0])
    assert header, f"first line must be the %% HLD|LLD header: {lines[0]!r}"
    body = [ln for ln in lines[1:] if not ln.strip().startswith("%%")]
    kind_line = body[0].strip()
    kind = kind_line.split()[0]
    info: dict = {
        "header_type": header.group(1),
        "title_words": len(header.group(2).split()),
        "kind": kind,
        "direction": kind_line.split()[1] if kind == "flowchart" else None,
    }
    labels: list[str] = []
    unlabelled: list[str] = []
    if kind == "flowchart":
        nodes: set[str] = set()
        edges = subgraphs = ends = 0
        classdefs: list[str] = []
        class_lines = tagged = 0
        for ln in body[1:]:
            s = ln.strip()
            if s.startswith("subgraph"):
                subgraphs += 1
            elif s == "end":
                ends += 1
            elif s.startswith("classDef"):
                classdefs.append(s.split()[1])
            elif re.match(r"class\s", s):
                class_lines += 1
            elif "-->" in s:
                m = _ARROW_FLOW.match(s)
                assert m, f"unparseable edge: {s!r}"
                edges += 1
                nodes.update({m.group(1), m.group(3)})
                if m.group(2) and m.group(2).strip():
                    labels.append(m.group(2).strip())
                else:
                    unlabelled.append(s)
            else:
                m = _NODE_DECL.match(s)
                if m:
                    nodes.add(m.group(1))
                    tagged += s.count(":::")
                    lm = _NODE_LABEL.match(s)
                    assert lm, f"unparseable node: {s!r}"
                    for part in re.split(r"<br/>", lm.group(1)):
                        assert len(part) <= 30, f"node label segment over 30 chars: {part!r}"
        info.update(
            nodes=len(nodes),
            edges=edges,
            subgraphs=subgraphs,
            ends=ends,
            classdefs=sorted(classdefs),
            class_assign_lines=class_lines,
            tagged=tagged,
        )
    elif kind == "sequenceDiagram":
        participants = msgs = notes = acts = deacts = 0
        for ln in body[1:]:
            s = ln.strip()
            if s.startswith("participant"):
                participants += 1
            elif s.startswith("Note"):
                notes += 1
            elif s.startswith("activate"):
                acts += 1
            elif s.startswith("deactivate"):
                deacts += 1
            else:
                m = _SEQ_MSG.match(s)
                assert m, f"unparseable sequence line: {s!r}"
                msgs += 1
                (labels if m.group(4) else unlabelled).append(m.group(4) or s)
        info.update(
            participants=participants, messages=msgs, notes=notes, activate=acts, deactivate=deacts
        )
    elif kind == "stateDiagram-v2":
        states: set[str] = set()
        transitions = terminal = 0
        for ln in body[1:]:
            m = _STATE_TR.match(ln.strip())
            assert m, f"unparseable state line: {ln.strip()!r}"
            transitions += 1
            ends_pair = {m.group(1), m.group(2)}
            states.update(ends_pair - {"[*]"})
            if "[*]" in ends_pair:
                terminal += 1  # start and end markers carry no label
            elif m.group(3):
                labels.append(m.group(3))
            else:
                unlabelled.append(ln.strip())
        info.update(states=len(states), transitions=transitions, start_end_transitions=terminal)
    else:
        raise AssertionError(f"unexpected diagram kind {kind!r}")
    info["unlabelled"] = unlabelled
    info["label_count"] = len(labels)
    info["longest_label"] = max((len(x) for x in labels), default=0)
    return info


ANALYSED = [_analyse(b) for b in BLOCKS]

EXPECTED_SHAPES = [
    {
        "header_type": "HLD",
        "kind": "flowchart",
        "direction": "LR",
        "nodes": 7,
        "edges": 6,
        "subgraphs": 3,
        "ends": 3,
        "classdefs": ["datastore", "external", "human", "service"],
        "class_assign_lines": 1,
        "tagged": 3,
        "label_count": 6,
    },
    {
        "header_type": "LLD",
        "kind": "sequenceDiagram",
        "participants": 4,
        "messages": 8,
        "notes": 2,
        "activate": 2,
        "deactivate": 2,
        "label_count": 8,
    },
    {
        "header_type": "LLD",
        "kind": "stateDiagram-v2",
        "states": 9,
        "transitions": 14,
        "start_end_transitions": 4,
        "label_count": 10,
    },
]


def test_three_mermaid_examples_in_order():
    assert len(BLOCKS) == 3


@pytest.mark.parametrize("index", range(3))
def test_mermaid_example_shape(index: int):
    got = ANALYSED[index]
    for key, want in EXPECTED_SHAPES[index].items():
        assert got[key] == want, f"example {index}: {key} is {got[key]!r}, expected {want!r}"


@pytest.mark.parametrize("index", range(3))
def test_mermaid_every_edge_has_a_label(index: int):
    assert ANALYSED[index]["unlabelled"] == []


@pytest.mark.parametrize("index", range(3))
def test_mermaid_header_and_label_limits(index: int):
    got = ANALYSED[index]
    assert got["title_words"] <= 8
    assert got["longest_label"] <= 40
    if got["kind"] == "flowchart":
        assert got["subgraphs"] == got["ends"]


def test_flowchart_classdefs_are_a_subset_of_the_rule_3_set():
    rule3 = set(re.findall(r"^\s{4}classDef (\w+) ", DIAGRAM_RULES_BLOCK.split("## 4.")[0], re.M))
    assert rule3 == {"service", "external", "datastore", "queue", "human"}
    assert set(ANALYSED[0]["classdefs"]) <= rule3
    tags = set(re.findall(r":::(\w+)", DIAGRAM_RULES_BLOCK))
    assert tags <= rule3


def test_ui_header_regex_matches_every_example_header():
    src = MERMAID_TSX.read_text()
    m = re.search(r"code\.match\(/(.+?)/m\)", src)
    assert m, "could not find the header regex in mermaid.tsx; update this test with the UI change"
    regex = re.compile(m.group(1), re.M)
    for index, block in enumerate(BLOCKS):
        hit = regex.search(block)
        assert hit, f"example {index} header is not matched by the UI regex"
        assert hit.group(1) == EXPECTED_SHAPES[index]["header_type"]
    # The prompt tells the model to emit exactly this shape.
    assert "%% <TYPE>: <Short Title>" in DIAGRAM_RULES_BLOCK


def test_diagram_rules_block_section_numbers_are_stable():
    heads = re.findall(r"^## (\d+b?)\. ", DIAGRAM_RULES_BLOCK, re.M)
    assert heads == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "9b", "10"]


# ── confidence policy ────────────────────────────────────────────────────────


def _tier_examples() -> dict[str, str]:
    rule2 = CODE_IS_TRUTH_POLICY.split("2. **Phrase claims")[1].split("3. **Evidence block")[0]
    out: dict[str, str] = {}
    for m in re.finditer(r"^\s+- (T\d): (.*?)(?=^\s+- T\d:|\Z)", rule2, re.M | re.S):
        out[m.group(1)] = _flat(m.group(2))
    return out


def test_tier_hierarchy_order():
    hier = CODE_IS_TRUTH_POLICY.split("## Tier hierarchy")[1].split("## Rules")[0]
    assert re.findall(r"^\s+(T\d)\s", hier, re.M) == ["T1", "T2", "T3", "T4", "T5"]


def test_tier_examples_order_and_shape():
    examples = _tier_examples()
    assert list(examples) == ["T1", "T2", "T3", "T4", "T5"]
    footnotes = []
    for tier in ("T1", "T2", "T3", "T4"):
        text = examples[tier]
        # T4 cites a documentation page, so it has a footnote but no code identifier.
        if tier != "T4":
            assert re.search(r"`[^`\s]+`", text), f"{tier} example has no backticked identifier"
        notes = re.findall(r"\[\^(\d+)\]", text)
        assert len(notes) == 1, f"{tier} example needs exactly one [^n] footnote"
        footnotes.append(int(notes[0]))
    assert footnotes == sorted(footnotes) == [1, 2, 3, 4]
    # T5 is deliberately the tier with no example: the policy says to avoid it.
    assert "[^" not in examples["T5"] and "`" not in examples["T5"]
    assert "Avoid" in examples["T5"]


def test_policy_rule_numbers():
    rules = CODE_IS_TRUTH_POLICY.split("## Rules")[1]
    assert re.findall(r"^(\d)\. \*\*", rules, re.M) == ["1", "2", "3", "4", "5"]


# ── synthesis prompt ─────────────────────────────────────────────────────────

SYNTH_HEADINGS = [
    "TL;DR",
    "How it works",
    "Key components",
    "End-to-end flow",
    "Cross-source alignment",
    "Source coverage",
    "Caveats & staleness",
    "Gaps & unknowns",
    "Sources",
]


def test_synthesis_prompt_section_headings_verbatim():
    assert re.findall(r"^\s{3}### (.+)$", SYNTH_BASE, re.M) == SYNTH_HEADINGS
    assert re.findall(r"^## (.+)$", SYNTH_BASE, re.M) == [
        "HARD RULES",
        "VERIFICATION DISCIPLINE",
        "ROUTING COMPLETENESS",
        "STRATEGIC FIT",
        "STYLE",
    ]
    assert re.findall(r"^### (.+)$", SYNTH_BASE, re.M) == ["DEPTH", "PARAGRAPH STRUCTURE"]
    assert "use these headings verbatim" in SYNTH_BASE


def test_synthesis_hard_rules_one_to_four():
    hard = SYNTH_BASE.split("## HARD RULES")[1].split("## VERIFICATION DISCIPLINE")[0]
    top = re.findall(r"^(\d)\. \*\*(.+?)\*\*", hard, re.M)
    assert [n for n, _ in top] == ["1", "2", "3", "4"]
    rule_text = {
        n: _flat(t) for n, t in re.findall(r"^(\d)\. (.*?)(?=^\d\. |\Z)", hard, re.M | re.S)
    }
    assert "T1" in rule_text["1"] and "T2" in rule_text["1"] and "T4" in rule_text["1"]
    assert "Hedge by tier" in rule_text["1"]
    assert "[^1]" in rule_text["2"] and "EVIDENCE-CONSISTENCY GUARD" in rule_text["2"]
    assert "BOUNDARY-PRESERVATION GUARD" in rule_text["2"]
    assert "URN" in rule_text["3"] and "Bad:" in rule_text["3"] and "Good:" in rule_text["3"]
    assert "verbatim" in rule_text["4"]


def test_boundary_guard_keeps_five_roles():
    guard = _flat(SYNTH_BASE.split("BOUNDARY-PRESERVATION GUARD")[1].split("3. **Prefer URNs")[0])
    for role in (
        "identity creation",
        "policy decision",
        "orchestration",
        "state mutation",
        "downstream sink",
    ):
        assert role in guard, role
    assert re.findall(r"\be\.g\. ", guard).__len__() >= 4


def test_rule_3_good_example_has_identifier_and_footnote():
    rule3 = _flat(SYNTH_BASE.split("3. **Prefer URNs")[1].split("4. **Follow")[0])
    good = rule3.split("Good:")[1]
    assert re.search(r"`[^`]+`\s*\[\^1\]", good) and "[^2]" in good


def test_sources_example_matches_scorer_and_ui_patterns():
    sources = SYNTH_BASE.split("### Sources")[1].split("## VERIFICATION DISCIPLINE")[0]
    footnotes = re.findall(r"^\s+\[\^(\d)\]: (.+)$", sources, re.M)
    assert [n for n, _ in footnotes] == ["1", "2"]
    assert scorer._URN_RE.search(footnotes[0][1]), (
        "first footnote example must carry a urn:bifrost: URN"
    )
    assert scorer._JIRA_RE.search(footnotes[1][1]), "second footnote example must carry a Jira key"
    assert scorer._FILE_LINE_RE.search(SYNTH_BASE), "DEPTH example must carry a path:line citation"


# ── couplings to code ────────────────────────────────────────────────────────

# The subset of coordinator._EVIDENCE_GAP_NEEDLES that the synthesis prompt spells out today.
# Other code matches these case-insensitively against the model's answer, so the prompt must
# keep emitting them byte for byte.
GAP_PHRASES_IN_PROMPT = (
    "search failed",
    "unavailable this turn",
    "timed out",
    "not routed",
    "not in scope",
    "not walked",
    "couldn't determine",
    "exact caller unknown",
    "documented but not code-verified",
    "treat as speculative",
    "first commit/pr not actually located",
)


def test_gap_needles_are_in_sync_with_the_scorer():
    assert tuple(coordinator._EVIDENCE_GAP_NEEDLES) == tuple(scorer._GAP_NEEDLES)


def test_gap_phrases_are_prompt_text_and_real_needles():
    flat = _flat(SYNTH).lower()
    for phrase in GAP_PHRASES_IN_PROMPT:
        assert phrase in coordinator._EVIDENCE_GAP_NEEDLES, f"{phrase!r} is no longer a needle"
        assert phrase in flat, f"synthesis prompt no longer contains the needle {phrase!r}"


def test_exact_caller_unknown_is_in_the_boundary_guard_and_cited_as_such():
    guard = _flat(SYNTH_BASE.split("BOUNDARY-PRESERVATION GUARD")[1])
    assert '**"exact caller unknown"**' in guard


def test_confidence_policy_still_names_the_documented_gap_phrase():
    assert "documented but not code-verified" in _flat(CODE_IS_TRUTH_POLICY)


def _strings_outside_prompt() -> str:
    """Every string literal in coordinator.py except the synthesis prompt assignments."""
    tree = ast.parse(COORDINATOR_PY.read_text())
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_SYNTHESIS_SYSTEM_PROMPT" for t in node.targets
        ):
            skip.update(id(n) for n in ast.walk(node))
    parts = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
    ]
    return _flat("\n".join(parts))


def test_input_section_names_in_the_prompt_are_emitted_by_the_user_message_builders():
    emitted = _strings_outside_prompt()
    for name in (
        "Architectural context — relevant communities",
        "Pre-computed flow paths",
        "Source coverage (audit trail)",
        "Source failures",
        "Code audit",
        "High-confidence",
        "Medium-confidence",
        "Low-confidence",
        "<prior_research>",
    ):
        assert name in _flat(SYNTH), f"{name!r} is not in the synthesis prompt"
        assert name in emitted, f"{name!r} is not emitted by any builder in coordinator.py"


def test_user_message_heading_chain_is_a_subsequence_of_the_prompt_headings():
    emitted = _strings_outside_prompt()
    m = re.search(r"mandated in the system prompt\*\* \((.*?)\)\.", emitted)
    assert m, "could not find the heading chain in the synthesis user message"
    chain = re.findall(r"### (.+?)(?= → |$)", m.group(1))
    assert chain and chain[0] == "TL;DR" and chain[-1] == "Sources"
    it = iter(SYNTH_HEADINGS)
    assert all(any(h == c for h in it) for c in chain), (
        f"{chain} is not an ordered subset of {SYNTH_HEADINGS}"
    )


def test_format_override_blocks_only_name_known_headings():
    allowed_extra = {"Key points"}
    for key, block in coordinator._FORMAT_DIRECTIVE_BLOCKS.items():
        for heading in re.findall(r"`### ([^`]+)`", block):
            heading = re.sub(r" \(.*$", "", heading)
            assert heading in SYNTH_HEADINGS or heading in allowed_extra, (
                f"{key}: unknown heading {heading!r}"
            )


def test_scorer_dossier_sections_exist_in_the_prompt():
    for lowered in scorer._DOSSIER_ONLY_SECTIONS:
        assert lowered.removeprefix("### ") in {h.lower() for h in SYNTH_HEADINGS}


def test_scorer_diagram_detection_matches_the_prompt_fences():
    assert re.search(r"```mermaid\b", DIAGRAM_RULES_BLOCK, re.IGNORECASE)


def _js_regex(src: str, anchor: str) -> re.Pattern[str]:
    """Pull the regex literal that contains ``anchor`` out of a TSX file and compile it for Python."""
    for m in re.finditer(r"/((?:\\/|[^/\n])+)/([gimsuy]*)", src):
        if anchor in m.group(1):
            flags = re.I if "i" in m.group(2) else 0
            return re.compile(m.group(1), flags)
    raise AssertionError(
        f"no regex literal containing {anchor!r}; update this test with the UI change"
    )


def test_ui_prose_splits_sources_and_tldr_from_a_prompt_shaped_answer():
    src = PROSE_TSX.read_text()
    sources_re = _js_regex(src, "Sources|Footnotes|Citations")
    tldr_re = _js_regex(src, "TL;?DR|Summary")
    answer = (
        "\n".join(f"### {h}\nbody text [^1]\n" for h in SYNTH_HEADINGS if h != "Sources")
        + "\n### Sources\n[^1]: `urn:bifrost:x` — y\n"
    )
    hit = sources_re.search("\n" + answer)
    assert hit and hit.group(1).strip() == "### Sources"
    tl = tldr_re.match(answer)
    assert tl and tl.group(2).strip() == "body text [^1]"
