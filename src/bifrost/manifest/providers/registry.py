"""Build the configured provider chain, in configured order."""

from __future__ import annotations

from bifrost.cli.config_sections import ManifestConfig
from bifrost.errors import ConfigError
from bifrost.manifest.providers.backstage import BackstageProvider
from bifrost.manifest.providers.base import ManifestProvider
from bifrost.manifest.providers.codeowners import CodeownersProvider
from bifrost.manifest.providers.generic_yaml import GenericYamlProvider
from bifrost.manifest.providers.none import NoneProvider


def build_providers(cfg: ManifestConfig) -> list[ManifestProvider]:
    providers: list[ManifestProvider] = []
    for name in cfg.providers:
        if name == "backstage":
            providers.append(BackstageProvider(cfg.backstage))
        elif name == "codeowners":
            providers.append(CodeownersProvider(cfg.codeowners))
        elif name == "generic_yaml":
            if not cfg.generic_yaml.path:
                raise ConfigError(
                    "manifest.generic_yaml.path is required when generic_yaml is enabled"
                )
            providers.append(GenericYamlProvider(cfg.generic_yaml))
        elif name == "none":
            providers.append(NoneProvider())
        else:
            raise ConfigError(f"Unknown manifest provider {name!r}")
    return providers
