"""Prompt text must stay disjoint from the AcmeCo benchmark domain.

The benchmark scores answers by substring needles from docs/benchmarks/acme.yml.
Few-shot examples in the same domain would inflate or contaminate those scores, so no
prompt constant may contain a needle, an expected entity, or an examples/acmeco name.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest

import bifrost
from bifrost.benchmarks.loader import REPO_ROOT, _load_yaml

ACMECO_DIR = REPO_ROOT / "examples" / "acmeco"
_NAME_SUFFIX = re.compile(r"(_PROMPT|_SYSTEM|_BLOCK|_POLICY)S?$")

# Pre-existing hits. Each entry is (constant name, lowercased term) and must carry a reason.
# Allowlisted entries may stop matching once a prompt is rewritten; that is fine.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "KNOWLEDGE_INDEX_SYSTEM_PROMPT",
        "exponential backoff",
    ): "generic key_facts example, pre-existing",
    ("KNOWLEDGE_INDEX_SYSTEM_PROMPT", "backoff"): "same phrase as the entry above",
}

# AcmeCo identifiers that are ordinary English or Python words; a hit would be noise.
GENERIC_ACME_IDENTIFIERS = {"Settings", "Target"}

# Example names used in the prompt few-shots. Each must stay disjoint from the benchmark.
NEUTRAL_TERMS = (
    "OrderRouter",
    "FulfillmentExecutor",
    "carrier-gateway",
    "SHIP",
    "SHIP-1234",
    "INV",
    "OPS",
    "OrderService",
    "FulfillmentService",
    "ShipHub",
    "#ship",
    "CustomerRegistry",
    "PolicyEngine",
    "OrderOrchestrator",
    "InventoryService",
    "AuditLedger",
    "AnalyticsSink",
    "OrderActionProcessor",
    "CarrierAddressApiController",
    "src/router/OrderRouter.kt",
    "urn:bifrost:component:carrier-gateway::OrderRouter",
    "ADV",
    "SupplierClient",
    "STOCK_GATEWAY_HOST",
    "stock-gateway",
    "StockLookupHandler",
    "StockCheckHandler",
    "StockCheckCache",
    "ItemChecker",
    "SupplierApi",
    "Supplier API",
    "Stock Check Runbook",
    "POST /stock_check/items",
    "POST /stock_check",
    "IN_STOCK | OUT_OF_STOCK | UNKNOWN",
    "CONFIRMED",
    "BACKORDERED",
    "PACKING",
    "stock available",
    "items allocated",
    "parcel shipped",
    "stock released",
    "stock restored",
    "backorder expired",
    "short-circuit if supplier down",
    "Stock checking",
    "OrderService stock check sequence",
    "routes by tenant plan / routes requests by tenant plan",
    "routes carrier requests by tenant plan",
    "tenant plan",
    "which tenant plans, why tenant plan was chosen as the routing axis",
    "adding a new tenant plan",
    "data-testid",
    "OrderSummary.tsx",
    "ItemVal, StockVal, StockCtl",
    "lookupStock",
    "StockCheckResponse",
    "GET /levels",
    "Customer",
    "carrier adapter",
    "OrderService / OS",
    "FulfillmentService / FF / FulfillSvc",
    "OrderPlacement, OrderPlacementService",
    "AddressDataValidator",
    "CarrierValidatorResource",
    "CachedCarrierValidator",
    "Carrier SOAP/REST",
    "SOAP GetRateInfo",
    "OrderStateMachine.process()",
    "OSM",
    "validateAddress",
    "POST /orders/place",
    "POST /carriers/validate/address",
    "POST /carriers/validate",
    "STOCK_VALIDATION",
    "partial stock",
    "out of stock",
    "OSM order state transitions",
    "plan-enabled checks",
    "stock-level mutation",
    "do callers have fallbacks or timeouts?",
    "Does the caller or end user feel the impact immediately or is it delayed",
    "served by the organisation",
    "data-residency or regulatory rules",
    "the organisation",
    "com.example.x.Y",
    "orders",
    "How does order flow work?",
    "Order Placement",
    "how does OrderService handle retries / retry logic for carriers",
    "retry orders",
    "order_status_cache, OrderService.get_status, tests/test_orders.py",
    "how fast does return processing happen / SLA for returns",
    "we need to add a carrier; orders/carriers; inventory/stock; words carrier, inventory, stock",
    "order-intake",
    "machine-orders / machine orders",
    "inventory-platform",
    "ShipHub / shiphub / ship-hub / ship_hub / SHIP HUB",
    "ship team, SHIP conversation, SHIP repos",
    "benchmark review",
    "INV-1234",
    '"SHIP", "INV", "OPS"',
    "Shipping domain",
    "PriceCatalog",
    "com.example.stock.X / com.example.<X> / @example/ / github.com/example/",
    "example.atlassian.net",
    "order fulfillment / Order fulfillment orchestration",
    "Shipment event reporting",
    "stock reservation",
    "tracking number allocation",
    "OrderRouter in order-intake calls X in shared-carrier-lib",
    "StockService / stock-service / stockservice",
    "ExampleApp.OrderRouter calls_service LedgerService",
    "ShipmentService",
    "CarrierRateService",
    "identity creation (e.g. CustomerRegistry)",
    "policy decision (e.g. PolicyEngine)",
    'orchestration (e.g. OrderOrchestrator), written "order orchestration"',
    "state mutation (e.g. InventoryService)",
    "downstream sink (e.g. AuditLedger, AnalyticsSink)",
)


def _needles_and_entities() -> tuple[set[str], set[str]]:
    data = _load_yaml("acme")
    needles: set[str] = set()
    entities: set[str] = set()
    for q in data["questions"]:
        for fact in q.get("expected_key_facts") or []:
            needle = str(fact.get("needle", "")).strip().lower()
            if needle:
                needles.add(needle)
        for ref in q.get("expected_jira_refs") or []:
            needles.add(str(ref).strip().lower())
        for urn in q.get("expected_entities") or []:
            urn = str(urn).strip().lower()
            entities.add(urn)
            entities.add(re.split(r"[:/]", urn)[-1])
        for name in q.get("scope_repo_names") or []:
            entities.add(str(name).strip().lower())
    return needles, entities


def _acmeco_identifiers() -> tuple[set[str], set[str]]:
    """(case-sensitive code identifiers, lowercase service and team names)."""
    idents: set[str] = set()
    names: set[str] = set()
    for path in sorted(ACMECO_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ACMECO_DIR)
        if len(rel.parts) > 1:
            names.add(rel.parts[0].lower())
        if (
            path.suffix not in {".py", ".ts", ".env", ".yaml", ".yml", ".md"}
            and path.name != "CODEOWNERS"
        ):
            continue
        text = path.read_text()
        idents.update(
            re.findall(r"^\s*(?:export\s+)?(?:class|interface|enum)\s+([A-Za-z_]\w*)", text, re.M)
        )
        idents.update(re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text))
        names.update(m.lower() for m in re.findall(r"@acmeco/([a-z0-9-]+)", text))
        names.update(m.lower() for m in re.findall(r"^\s*name:\s*([a-z0-9-]+)\s*$", text, re.M))
    names.update({"acmeco", "dlvr", "plat", "webhook"})
    idents = {
        i for i in idents if not i.startswith("BIFROST_")
    }  # the app's own env, not the demo's
    return idents - GENERIC_ACME_IDENTIFIERS, names


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _prompt_texts() -> dict[str, str]:
    """{qualified name: text} for every module-level prompt constant plus known builders."""
    out: dict[str, str] = {}
    failures: list[str] = []
    for info in pkgutil.walk_packages(bifrost.__path__, "bifrost."):
        try:
            mod = importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001 - report every module that cannot be imported
            failures.append(f"{info.name}: {exc!r}")
            continue
        for name, value in vars(mod).items():
            if not _NAME_SUFFIX.search(name):
                continue
            if isinstance(value, str):
                out[f"{info.name}.{name}"] = value
            elif isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
                for key, text in value.items():
                    out[f"{info.name}.{name}[{key}]"] = text
            elif (
                isinstance(value, (list, tuple))
                and value
                and all(isinstance(v, str) for v in value)
            ):
                out[f"{info.name}.{name}"] = "\n".join(value)
    assert not failures, f"modules failed to import: {failures}"

    from bifrost.collaboration import handover_generator

    out["bifrost.collaboration.handover_generator._section_regen_system_prompt()"] = (
        handover_generator._section_regen_system_prompt("TL;DR")
    )
    return out


PROMPTS = _prompt_texts()
NEEDLES, ENTITIES = _needles_and_entities()
IDENTS, NAMES = _acmeco_identifiers()


def _hits(qualified: str, text: str) -> set[str]:
    const = qualified.split(".")[-1].split("[")[0].removesuffix("()")
    text = " ".join(text.split())  # prompts hard-wrap lines; an LLM answer would not
    lower = text.lower()
    compact = _compact(text)
    found: set[str] = set()
    for term in NEEDLES | ENTITIES:
        if term in lower:
            found.add(term)
    for name in NAMES:
        if re.search(rf"\b{re.escape(name)}\b", lower) or (
            len(name) > 5 and _compact(name) in compact
        ):
            found.add(name)
    for ident in IDENTS:
        if re.search(rf"\b{re.escape(ident)}\b", text):
            found.add(ident)
    return {t for t in found if (const, t.lower()) not in ALLOWED}


def test_walker_found_the_prompt_constants():
    # Guards against a silent walk: the repo has 50+ such constants today.
    assert len(PROMPTS) >= 45
    for expected in (
        "bifrost.research.coordinator._SYNTHESIS_SYSTEM_PROMPT",
        "bifrost.research.diagram_standards.DIAGRAM_RULES_BLOCK",
        "bifrost.research.confidence_policy.CODE_IS_TRUTH_POLICY",
        "bifrost.extraction.prompts.KNOWLEDGE_INDEX_SYSTEM_PROMPT",
    ):
        assert expected in PROMPTS


def test_acme_sets_are_not_empty():
    assert len(NEEDLES) >= 25 and "dispatcher" in NEEDLES
    assert {"webhook-api", "delivery-gateway"} <= ENTITIES
    assert {"DeliveryService", "DISPATCHER".replace("DISPATCHER", "DispatchWorker")} & IDENTS
    assert "DELIVERY_QUEUE" in IDENTS
    assert {"dispatcher", "signing-keys"} <= NAMES


@pytest.mark.parametrize("qualified", sorted(PROMPTS))
def test_prompt_has_no_acme_terms(qualified: str):
    hits = _hits(qualified, PROMPTS[qualified])
    assert not hits, f"{qualified} contains AcmeCo benchmark terms: {sorted(hits)}"


def test_neutral_terms_are_disjoint_from_acme():
    problems: list[str] = []
    for term in NEUTRAL_TERMS:
        lower = term.lower()
        for bad in NEEDLES | ENTITIES:
            if bad in lower:
                problems.append(f"{term!r} contains acme term {bad!r}")
        for bad in NAMES:
            if re.search(rf"\b{re.escape(bad)}\b", lower):
                problems.append(f"{term!r} contains acme name {bad!r}")
        for ident in IDENTS:
            if re.search(rf"\b{re.escape(ident)}\b", term):
                problems.append(f"{term!r} contains acmeco identifier {ident!r}")
        if re.search(r"\bregion\b", lower):
            problems.append(f"{term!r} uses 'region' (acme q6 is the region question)")
    assert not problems, problems
