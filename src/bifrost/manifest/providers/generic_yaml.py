"""Config-driven provider for any YAML manifest shape (e.g. a team's own service.yml)."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from bifrost.cli.config_sections import GenericYamlConfig
from bifrost.manifest.env_miner import MinedDeps, mine_mapping
from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest

logger = logging.getLogger(__name__)

NAME = "generic_yaml"
_STR_FIELDS = frozenset(
    {
        "name",
        "team",
        "description",
        "tier",
        "lifecycle",
        "system",
        "region",
        "data_classification",
        "jira_project_key",
        "confluence_space_key",
        "slack_channel",
    }
)
_LIST_FIELDS = frozenset({"owners", "compliance", "declared_dependencies"})
_BOOL_FIELDS = frozenset({"is_primary"})
_MISSING = object()


def _lookup(doc: dict[str, Any], dotted: str) -> Any:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _scalar(value: Any) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip() or None
    return None


def _segments_match(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_segments_match(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and head in ("*", path[0]) and _segments_match(rest, path[1:])


def _iter_dict_nodes(node: Any, prefix: list[str]):
    if isinstance(node, dict):
        yield prefix, node
        for key, value in node.items():
            yield from _iter_dict_nodes(value, [*prefix, str(key)])


def parse_generic_yaml(text: str, cfg: GenericYamlConfig) -> ServiceManifest | None:
    unknown = [f for f in cfg.fields if f not in _STR_FIELDS | _LIST_FIELDS | _BOOL_FIELDS]
    if unknown:
        raise ValueError(f"generic_yaml.fields has unknown manifest field(s) {unknown}")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("generic_yaml parse failed: %s", exc)
        return None
    if not isinstance(doc, dict):
        return None

    # Config blocks hold env values (possibly secrets); keep only what mining extracts.
    mined_roots = {p.split(".", 1)[0] for p in cfg.config_blocks}
    md = ServiceManifest(
        providers=[NAME], raw={NAME: {k: v for k, v in doc.items() if k not in mined_roots}}
    )
    for target, dotted in cfg.fields.items():
        value = _lookup(doc, dotted)
        if value is _MISSING:
            continue
        if target in _STR_FIELDS:
            setattr(md, target, _scalar(value))
        elif target in _BOOL_FIELDS:
            setattr(md, target, bool(value))
        else:
            items = value if isinstance(value, list) else [value]
            setattr(md, target, [s for s in (_scalar(i) for i in items) if s])

    mined = MinedDeps()
    for pattern in cfg.config_blocks:
        pat = pattern.split(".")
        for path, node in _iter_dict_nodes(doc, []):
            if path and _segments_match(pat, path):
                mine_mapping(node, mined)
    mined.apply_to(md)
    return md


class GenericYamlProvider:
    name = NAME

    def __init__(self, cfg: GenericYamlConfig) -> None:
        self.cfg = cfg

    async def load(self, files: RepoFiles, *, repo_name: str) -> ServiceManifest | None:
        text = await files.read_text(self.cfg.path)
        if text is None:
            return None
        md = parse_generic_yaml(text, self.cfg)
        if md is not None:
            md.source_files.append(self.cfg.path)
        return md
