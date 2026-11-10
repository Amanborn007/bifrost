"""Canonical enums for Bifrost — source of truth per TRD §2."""

from __future__ import annotations

from enum import StrEnum


class ConfidenceLevel(StrEnum):
    """Canonical confidence taxonomy for all knowledge artifacts and agent responses.

    Used in entity metadata, task results, agent responses, and the orchestration
    confidence gate.
    """

    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    STALE = "stale"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"
    CONFLICTING = "conflicting"


class ComponentType(StrEnum):
    MODULE = "module"
    SERVICE = "service"
    LIBRARY = "library"
    PACKAGE = "package"


class InterfaceType(StrEnum):
    API = "api"
    EVENT = "event"
    SCHEMA = "schema"
    CONFIG = "config"


class FlowType(StrEnum):
    REQUEST_RESPONSE = "request-response"
    EVENT_DRIVEN = "event-driven"
    BATCH = "batch"
    SAGA = "saga"
    PIPELINE = "pipeline"
    MIXED = "mixed"


class SegmentType(StrEnum):
    SYNC_CALL = "sync-call"
    ASYNC_MESSAGE = "async-message"
    DB_READ = "db-read"
    DB_WRITE = "db-write"
    CACHE_READ = "cache-read"
    CACHE_WRITE = "cache-write"
    EXTERNAL_API = "external-api"


class DependencyType(StrEnum):
    CODE = "code"
    API = "api"
    SCHEMA = "schema"
    CONFIG = "config"
    EVENT = "event"
    RUNTIME = "runtime"
    OWNERSHIP = "ownership"
    MIGRATION = "migration"
    INFERRED_HIDDEN = "inferred-hidden"


class DiscoveryMethod(StrEnum):
    STATIC_ANALYSIS = "static-analysis"
    SPEC_PARSING = "spec-parsing"
    RUNTIME_OBSERVATION = "runtime-observation"
    MANUAL = "manual"
    INFERRED = "inferred"


class SourceObjectType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    COMMIT = "commit"
    PULL_REQUEST = "pull-request"
    ISSUE = "issue"
    PAGE = "page"
    MESSAGE = "message"


class ArtifactClassification(StrEnum):
    REPO_OVERVIEW = "repo-overview"
    DOMAIN_SUMMARY = "domain-summary"
    FLOW_DOCUMENTATION = "flow-documentation"
    ONBOARDING_GUIDE = "onboarding-guide"
    IMPLEMENTATION_PLAYBOOK = "implementation-playbook"
    MIGRATION_GUIDE = "migration-guide"
    DEPENDENCY_ANALYSIS = "dependency-analysis"
    RISK_ASSESSMENT = "risk-assessment"
    PATTERN_ANALYSIS = "pattern-analysis"
    DECISION_RECORD = "decision-record"
    CUSTOM = "custom"


class CorrectionType(StrEnum):
    FACTUAL_ERROR = "factual-error"
    OUTDATED_CLAIM = "outdated-claim"
    MISSING_CONTEXT = "missing-context"
    WRONG_CONFIDENCE = "wrong-confidence"
    HALLUCINATION = "hallucination"
