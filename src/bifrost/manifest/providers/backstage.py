"""Backstage ``catalog-info.yaml`` provider."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from bifrost.cli.config_sections import BackstageConfig
from bifrost.manifest.files import RepoFiles
from bifrost.manifest.model import ServiceManifest
from bifrost.manifest.providers.base import first_existing

logger = logging.getLogger(__name__)

NAME = "backstage"
_MAPPABLE = frozenset(
    {
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
_JIRA_ANNOTATION = "jira/project-key"


def _text(value: Any) -> str | None:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        s = str(value).strip()
        return s or None
    return None


def _entity_ref(ref: str) -> tuple[str | None, str]:
    """``kind:ns/name`` | ``kind:name`` | ``ns/name`` | ``name`` -> (kind, name)."""
    head, sep, tail = ref.strip().partition(":")
    kind = head.lower() if sep else None
    return kind, (tail if sep else head).rsplit("/", 1)[-1]


def _apply_map(md: ServiceManifest, source: Any, mapping: dict[str, str], where: str) -> None:
    for key, target in mapping.items():
        if target not in _MAPPABLE:
            raise ValueError(f"backstage {where} maps {key!r} to unknown manifest field {target!r}")
    if not isinstance(source, dict):
        return
    for key, target in mapping.items():
        value = _text(source.get(key))
        if value and not getattr(md, target):
            setattr(md, target, value)


def parse_catalog_info(
    text: str, repo_name: str, cfg: BackstageConfig | None = None
) -> ServiceManifest | None:
    cfg = cfg or BackstageConfig()
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        logger.warning("catalog-info.yaml parse failed for %s: %s", repo_name, exc)
        return None
    components = [d for d in docs if str(d.get("kind", "")).lower() == "component"]
    if not components:
        return None

    def _meta_name(doc: dict[str, Any]) -> str | None:
        meta = doc.get("metadata")
        return _text(meta.get("name")) if isinstance(meta, dict) else None

    doc = next((d for d in components if _meta_name(d) == repo_name), components[0])
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}

    md = ServiceManifest(
        name=_meta_name(doc),
        description=_text(meta.get("description")),
        lifecycle=_text(spec.get("lifecycle")),
        system=_text(spec.get("system")),
        providers=[NAME],
        raw={NAME: doc},
    )

    owner = _text(spec.get("owner"))
    if owner:
        kind, name = _entity_ref(owner)
        if kind == "user":
            md.owners.append(name)
        else:
            md.team = name

    depends = spec.get("dependsOn")
    for ref in depends if isinstance(depends, list) else []:
        if not isinstance(ref, str):
            continue
        kind, name = _entity_ref(ref)
        if kind in (None, "component") and name not in md.declared_dependencies:
            md.declared_dependencies.append(name)

    annotations = meta.get("annotations")
    jira = _text(annotations.get(_JIRA_ANNOTATION)) if isinstance(annotations, dict) else None
    if jira:
        md.jira_project_key = jira
    _apply_map(md, annotations, cfg.annotation_map, "annotation_map")
    _apply_map(md, meta.get("labels"), cfg.label_map, "label_map")
    return md


class BackstageProvider:
    name = NAME

    def __init__(self, cfg: BackstageConfig) -> None:
        self.cfg = cfg

    async def load(self, files: RepoFiles, *, repo_name: str) -> ServiceManifest | None:
        found = await first_existing(files, self.cfg.paths)
        if found is None:
            return None
        rel, text = found
        md = parse_catalog_info(text, repo_name, self.cfg)
        if md is not None:
            md.source_files.append(rel)
        return md
