"""Mine dependency facts (hosts, topics/queues, database names) from config files.

Only matched keys and scrubbed values are kept; whole files are never stored.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from bifrost.manifest.model import ServiceManifest

logger = logging.getLogger(__name__)

_HOST_KEY = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*?)_(HOST|URL|ENDPOINT|SERVICE|GATEWAY|BASE_URL|API_BASE)$",
    re.IGNORECASE,
)
_TOPIC_KEY = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*?)_(TOPIC|QUEUE|TOPIC_ARN|QUEUE_URL|SNS|SQS|KAFKA)$",
    re.IGNORECASE,
)
_SERVICE_VALUE = re.compile(r"^(https?://|[a-z][a-z0-9-]*\.[a-z0-9-.]+)", re.IGNORECASE)
_NEGATIVE_PREFIX = re.compile(r"^(SECRET|CLIENT|API|PUBLIC|PRIVATE|OAUTH)_", re.IGNORECASE)
_JDBC = re.compile(r"jdbc:[a-z]+://[^/]+/([A-Za-z][A-Za-z0-9_]+)")
_DB_URL = re.compile(
    r"(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|sqlserver)://[^/\s]*/([A-Za-z][A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_USERINFO = re.compile(r"(?<=://)[^/@\s]*@")
_DOTENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

_HELM_ENV_KEYS = frozenset({"env", "extraenv", "extra_env", "envvars", "environment", "config"})


@dataclass
class MinedDeps:
    downstream_services: dict[str, str] = field(default_factory=dict)
    messaging_topics: dict[str, str] = field(default_factory=dict)
    topic_values: dict[str, set[str]] = field(default_factory=dict)
    database_names: list[str] = field(default_factory=list)

    def apply_to(self, md: ServiceManifest) -> None:
        for name, host in self.downstream_services.items():
            md.downstream_services.setdefault(name, host)
        for name, value in self.messaging_topics.items():
            md.messaging_topics.setdefault(name, value)
        for name, values in self.topic_values.items():
            md.topic_values.setdefault(name, set()).update(values)
        for db in self.database_names:
            if db not in md.database_names:
                md.database_names.append(db)


def scrub_value(value: str) -> str:
    """Strip ``user[:password]@`` from the authority of any URL in ``value``."""
    return _USERINFO.sub("", value)


def mine_mapping(mapping: Mapping[Any, Any], into: MinedDeps) -> None:
    """Extract *_HOST/*_URL services, *_TOPIC/*_QUEUE topics and database names."""
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            continue
        value_str = scrub_value(str(value))

        m_topic = _TOPIC_KEY.match(key)
        if m_topic:
            name = m_topic.group(1).lower()
            into.messaging_topics.setdefault(name, value_str)
            into.topic_values.setdefault(name, set()).add(value_str)
            continue

        m_host = _HOST_KEY.match(key)
        if m_host and not _NEGATIVE_PREFIX.match(key) and _SERVICE_VALUE.match(value_str):
            into.downstream_services.setdefault(m_host.group(1).lower(), value_str)
            continue

        for pattern in (_JDBC, _DB_URL):
            for match in pattern.finditer(value_str):
                db = match.group(1).lower()
                if db not in into.database_names:
                    into.database_names.append(db)


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()
        key, sep, rest = line.partition("=")
        key = key.strip()
        if not sep or not _DOTENV_KEY.match(key):
            continue
        out[key] = _dotenv_value(rest.strip())
    return out


def _dotenv_value(rest: str) -> str:
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        while end > 0 and quote == '"' and rest[end - 1] == "\\":
            end = rest.find(quote, end + 1)
        if end > 0:
            inner = rest[1:end]
            return inner.replace('\\"', '"') if quote == '"' else inner
    comment = re.search(r"\s#", rest)
    return (rest[: comment.start()] if comment else rest).strip()


def _load_yaml(text: str, what: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning("%s parse failed: %s", what, exc)
        return None


def iter_compose_env(text: str) -> Iterator[tuple[str, str]]:
    doc = _load_yaml(text, "compose file")
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return
    for svc in services.values():
        env = svc.get("environment") if isinstance(svc, dict) else None
        if isinstance(env, list):
            for item in env:
                if isinstance(item, str) and "=" in item:
                    k, _, v = item.partition("=")
                    yield k.strip(), v.strip()
        elif isinstance(env, dict):
            for k, v in env.items():
                if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                    yield k, str(v)


def _walk_helm(node: Any) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _HELM_ENV_KEYS:
                yield from _helm_env_entries(value)
            yield from _walk_helm(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_helm(item)


def _helm_env_entries(value: Any) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and isinstance(v, (str, int, float, bool)):
                yield k, str(v)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                v = item.get("value")
                if isinstance(v, (str, int, float, bool)):
                    yield item["name"], str(v)


def iter_helm_env(text: str) -> Iterator[tuple[str, str]]:
    yield from _walk_helm(_load_yaml(text, "helm values"))


def mine_file(rel: str, text: str, into: MinedDeps) -> None:
    """Dispatch on the file's basename; unknown file types are ignored."""
    base = rel.rsplit("/", 1)[-1].lower()
    is_yaml = base.endswith((".yaml", ".yml"))
    if base.endswith(".env") or base.startswith(".env") or base == "env":
        mine_mapping(parse_dotenv(text), into)
    elif is_yaml and (base.startswith("docker-compose") or base.startswith("compose.")):
        for key, value in iter_compose_env(text):
            mine_mapping({key: value}, into)
    elif is_yaml and base.startswith("values"):
        for key, value in iter_helm_env(text):
            mine_mapping({key: value}, into)
