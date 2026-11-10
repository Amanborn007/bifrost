"""Nested sections of ``bifrost.yaml``.

Each model maps one top-level YAML key onto ``BifrostSettings``. Sections
reject unknown keys so a typo in the YAML fails at boot instead of being
silently ignored.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from bifrost.errors import ConfigError

__all__ = [
    "ApiTokenConfig",
    "BackstageConfig",
    "CodeownersConfig",
    "ConfigError",
    "ConfluenceSourceConfig",
    "ConventionsConfig",
    "GenericYamlConfig",
    "GitHubSourceConfig",
    "IdentityConfig",
    "IdentityHeaderConfig",
    "IdentityOidcConfig",
    "JiraSourceConfig",
    "LinkingConfig",
    "LocalSourceConfig",
    "ManifestConfig",
    "McpConfig",
    "OrgConfig",
    "SourcesConfig",
]

_MCP_PREFIX_RE = re.compile(r"^mcp__[\w-]+__$")
_ATLASSIAN_CLOUD_HOST = r"[\w-]+\.atlassian\.net"
_MCP_TOKEN_RE = re.compile(r"\{mcp:([a-z_]+):([\w-]+)\}")
_EPIC_LINK_FIELD_RE = re.compile(r"^customfield_\d+$")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrgConfig(_Section):
    name: str = ""


class GitHubSourceConfig(_Section):
    host: str = "github.com"
    orgs: list[str] = Field(default_factory=list)


class JiraSourceConfig(_Section):
    base_url: str = ""
    # Custom Epic Link field of an older Jira; unset uses the standard ``parent`` only.
    epic_link_field: str | None = None
    # Live-fetch credentials; both or neither. Env: BIFROST_SOURCES__JIRA__API_TOKEN.
    email: str | None = None
    api_token: SecretStr | None = None

    @field_validator("epic_link_field")
    @classmethod
    def _check_epic_link_field(cls, value: str | None) -> str | None:
        if value is not None and not _EPIC_LINK_FIELD_RE.match(value):
            raise ConfigError(
                f"sources.jira.epic_link_field {value!r} must match {_EPIC_LINK_FIELD_RE.pattern}"
            )
        return value

    @model_validator(mode="after")
    def _credentials_come_together(self) -> JiraSourceConfig:
        if (self.email is None) != (self.api_token is None):
            raise ConfigError("sources.jira.email and sources.jira.api_token must be set together")
        return self

    def host_pattern(self, default: str = _ATLASSIAN_CLOUD_HOST) -> str:
        """Regex alternation (no scheme) of the configured Jira host and ``default``."""
        alternatives: list[str] = []
        if self.base_url:
            parsed = urlparse(self.base_url if "//" in self.base_url else f"//{self.base_url}")
            if not parsed.netloc:
                raise ConfigError(f"sources.jira.base_url {self.base_url!r} has no host")
            alternatives.append(re.escape(parsed.netloc + parsed.path.rstrip("/")))
        alternatives.append(default)
        return f"(?:{'|'.join(alternatives)})"

    def browse_url(self, key: str | None) -> str | None:
        """Browse URL for an issue key, or None without a key or a configured base URL."""
        if not key or not self.base_url:
            return None
        return f"{self.base_url.rstrip('/')}/browse/{key}"


class ConfluenceSourceConfig(_Section):
    # Self-hosted Confluence root, e.g. https://wiki.example.com; Atlassian Cloud always matches.
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"sources.confluence.base_url {value!r} must be an http(s) URL with a host"
            )
        return value

    def url_pattern(self, cloud: str) -> str:
        """Regex alternation of the Cloud pattern ``cloud`` and the configured base URL."""
        if self.base_url is None:
            return cloud
        return f"(?:{cloud}|{re.escape(self.base_url.rstrip('/'))})"


class LocalSourceConfig(_Section):
    allowed_roots: list[str] = Field(default_factory=list)


class SourcesConfig(_Section):
    github: GitHubSourceConfig = Field(default_factory=GitHubSourceConfig)
    jira: JiraSourceConfig = Field(default_factory=JiraSourceConfig)
    confluence: ConfluenceSourceConfig = Field(default_factory=ConfluenceSourceConfig)
    local: LocalSourceConfig = Field(default_factory=LocalSourceConfig)


def _check_mcp_prefix(value: str) -> str:
    if not _MCP_PREFIX_RE.match(value):
        raise ValueError(f"MCP tool prefix {value!r} must match {_MCP_PREFIX_RE.pattern}")
    return value


McpPrefix = Annotated[str, AfterValidator(_check_mcp_prefix)]

_DEFAULT_MCP_PREFIXES: dict[str, str] = {
    "atlassian": "mcp__atlassian__",
    "github": "mcp__github__",
    "slack": "mcp__claude_ai_Slack__",
    "slack_write": "mcp__plugin_slack_slack__",
    "gdrive": "mcp__claude_ai_Google_Drive__",
}


def is_mcp_tool_name(name: str) -> bool:
    """True for a fully-qualified MCP tool name (as opposed to a built-in like ``Read``)."""
    return name.startswith("mcp__")


class McpConfig(_Section):
    tool_prefix: dict[str, McpPrefix] = Field(default_factory=lambda: dict(_DEFAULT_MCP_PREFIXES))

    @field_validator("tool_prefix", mode="before")
    @classmethod
    def _merge_defaults(cls, value: object) -> object:
        # A partial override (one server) must not drop the other servers' prefixes.
        if isinstance(value, dict):
            return {**_DEFAULT_MCP_PREFIXES, **value}
        return value

    def tools(self, server: str, names: Iterable[str]) -> list[str]:
        """Fully-qualified names for one server's tools, in the order given."""
        qualified = [self.tool(server, name) for name in names]
        if not qualified:
            raise ConfigError(f"No tool names given for MCP server {server!r}")
        return qualified

    def render(self, template: str) -> str:
        """Replace every ``{mcp:<server>:<name>}`` token with its qualified tool name."""
        return _MCP_TOKEN_RE.sub(lambda m: self.tool(m.group(1), m.group(2)), template)

    def tool(self, server: str, name: str) -> str:
        """Fully-qualified MCP tool name, e.g. ``tool("atlassian", "getJiraIssue")``."""
        try:
            prefix = self.tool_prefix[server]
        except KeyError:
            raise ConfigError(
                f"Unknown MCP server {server!r}; configured: {sorted(self.tool_prefix)}"
            ) from None
        return f"{prefix}{name}"


MANIFEST_PROVIDERS = frozenset({"none", "backstage", "codeowners", "generic_yaml"})


class BackstageConfig(_Section):
    paths: list[str] = Field(default_factory=lambda: ["catalog-info.yaml", "catalog-info.yml"])
    annotation_map: dict[str, str] = Field(default_factory=dict)
    label_map: dict[str, str] = Field(default_factory=dict)


class CodeownersConfig(_Section):
    paths: list[str] = Field(
        default_factory=lambda: ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]
    )
    strip_org_prefix: bool = True


class GenericYamlConfig(_Section):
    path: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    config_blocks: list[str] = Field(default_factory=list)


class ManifestConfig(_Section):
    providers: list[str] = Field(default_factory=lambda: ["backstage", "codeowners"])
    config_globs: list[str] = Field(
        default_factory=lambda: [
            "config/*.env",
            ".env.example",
            "docker-compose*.y*ml",
            "helm/**/values*.yaml",
            "charts/**/values*.yaml",
        ]
    )
    remote_config_globs: bool = False
    max_file_bytes: int = Field(default=262144, ge=1)
    backstage: BackstageConfig = Field(default_factory=BackstageConfig)
    codeowners: CodeownersConfig = Field(default_factory=CodeownersConfig)
    generic_yaml: GenericYamlConfig = Field(default_factory=GenericYamlConfig)

    @field_validator("providers")
    @classmethod
    def _known_providers(cls, value: list[str]) -> list[str]:
        unknown = [p for p in value if p not in MANIFEST_PROVIDERS]
        if unknown:
            raise ValueError(
                f"Unknown manifest provider(s) {unknown}; known: {sorted(MANIFEST_PROVIDERS)}"
            )
        if "none" in value and len(value) > 1:
            raise ValueError("Manifest provider 'none' must be the only entry when present")
        return value


class LinkingConfig(_Section):
    internal_package_prefixes: list[str] = Field(default_factory=list)


class ConventionsConfig(_Section):
    jira_project_key: str = "{TEAM}"
    confluence_space_key: str = "{TEAM}"
    slack_channel: str = "#{team}"
    placeholder_teams: list[str] = Field(default_factory=list)


_LOOPBACK_CIDRS = ("127.0.0.0/8", "::1/128")
_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class IdentityHeaderConfig(_Section):
    email: str = "X-Forwarded-Email"
    groups: str = "X-Forwarded-Groups"
    # Peers allowed to set the identity headers; the rest are stripped.
    trusted_proxy_cidrs: list[str] = Field(default_factory=lambda: list(_LOOPBACK_CIDRS))

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _valid_cidrs(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("trusted_proxy_cidrs must list at least one CIDR")
        for cidr in value:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"trusted_proxy_cidrs entry {cidr!r} is not a CIDR") from exc
        return value


class IdentityOidcConfig(_Section):
    jwks_url: str = ""
    issuer: str = ""
    audience: str = ""
    groups_claim: str = "groups"


class IdentityConfig(_Section):
    provider: Literal["none", "header", "oidc_jwt"] = "none"
    # header mode trusts proxy-supplied headers, so it must be opted into explicitly.
    trusted_proxy: bool = False
    demo_email: str = "demo@example.com"
    header: IdentityHeaderConfig = Field(default_factory=IdentityHeaderConfig)
    oidc: IdentityOidcConfig = Field(default_factory=IdentityOidcConfig)
    # IdP groups that grant the admin role. Required under header and oidc_jwt;
    # an explicit ``{admin: []}`` means admin comes from API tokens only.
    roles: dict[Literal["admin"], list[str]] | None = None


class ApiTokenConfig(_Section):
    """One ``bft_`` API token, stored as the sha256 of the secret."""

    name: str
    sha256: str
    roles: list[Literal["member", "admin"]] = Field(default_factory=lambda: ["member"])

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _TOKEN_NAME_RE.match(value):
            raise ValueError(f"api token name {value!r} must match {_TOKEN_NAME_RE.pattern}")
        return value

    @field_validator("sha256")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        digest = value.strip().lower()
        if not _SHA256_RE.match(digest):
            raise ValueError("api token sha256 must be 64 lowercase hex characters")
        return digest
