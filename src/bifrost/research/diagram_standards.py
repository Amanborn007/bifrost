"""Canonical diagram standards — injected into any Bifrost prompt that
emits Mermaid diagrams.

Goals:
  * Consistent visual language across all generated answers (users
    learn the diagram grammar once).
  * Enforce HLD vs LLD distinction so the right diagram type is chosen
    for the audience + question.
  * Enforce labeled edges — no bare arrows — so diagrams survive
    being viewed without the prose.
  * Consistent styling classes so colours, shapes, and grouping stay
    stable across answers.

Import ``DIAGRAM_RULES_BLOCK`` into any system prompt where a diagram
might be generated. The block is ~1800 tokens — small enough to
include in every request, large enough to actually constrain output.
"""

from __future__ import annotations

DIAGRAM_RULES_BLOCK = """\
# ─────────────────────────────────────────────────────────────────
# CANONICAL DIAGRAM STANDARDS (apply to every mermaid block you emit)
# ─────────────────────────────────────────────────────────────────

All diagrams MUST follow these rules. The UI renders them consistently,
and users have learned this visual grammar — deviating hurts readability.

## 1. DECIDE HLD vs LLD FIRST

Before emitting any diagram, decide which level fits the question:

* **HLD (High-Level Design)** — zoom level = whole system / multiple
  services. Shows service boundaries, data flow between services,
  ownership. Use when the question spans 2+ services OR the answer
  is architecture-oriented. Diagram type: `flowchart TB` / `graph LR`.

* **LLD (Low-Level Design)** — zoom level = inside a single service
  or a specific flow. Shows method calls, class collaborations,
  specific API routes, data structures. Use when the question is
  about implementation detail inside a bounded context. Diagram
  type: `sequenceDiagram` (for temporal flow) or `stateDiagram-v2`
  (for state machines).

If an answer has BOTH levels of concern, emit TWO diagrams — one HLD
first for orientation, then one LLD for the specific mechanism. Never
try to cram both into a single diagram.

## 2. REQUIRED HEADER DIRECTIVE

Every diagram MUST open with a %% comment line declaring the type
AND a short title. The UI surfaces this as the diagram label:

    %% HLD: Stock checking — service orchestration
    %% LLD: OrderService stock check sequence
    %% LLD: Order state transitions

Exactly this format: `%% <TYPE>: <Short Title>`. Title ≤ 8 words.

## 3. REQUIRED STYLING CLASSES

Append these classDef lines at the END of every HLD flowchart/graph
so internal vs external vs infra are visually distinct:

    classDef service fill:#1e3a5f,stroke:#3b82f6,stroke-width:1.5px,color:#e5e7eb
    classDef external fill:#374151,stroke:#9ca3af,stroke-width:1px,color:#e5e7eb,stroke-dasharray:4 3
    classDef datastore fill:#422006,stroke:#d97706,stroke-width:1px,color:#fef3c7
    classDef queue fill:#3b0764,stroke:#a855f7,stroke-width:1px,color:#e9d5ff
    classDef human fill:#064e3b,stroke:#10b981,stroke-width:1px,color:#d1fae5

Then tag each node with its class:

    OrderSvc:::service
    SupplierApi:::external
    DynamoDB:::datastore
    SQS:::queue
    Customer:::human

For sequenceDiagram, use Note over/right of/left of liberally to
annotate edges; don't rely on classes (sequence diagrams have
their own look).

## 4. EDGE LABELS ARE MANDATORY

Never use a bare `-->` or `->>`. Every edge MUST carry either a label
or a %% comment explaining what travels across it:

    Bad:   OrderSvc --> FulfillSvc
    Good:  OrderSvc -->|POST /stock_check| FulfillSvc

For sequence diagrams:

    Bad:   Client->>OrderSvc: msg
    Good:  Client->>OrderSvc: POST /orders/submit

The label should name the CONCRETE thing crossing the boundary — an
HTTP route, an event name, a method signature — not a vague verb.

## 5. KEEP LABELS SHORT

Node labels ≤ 30 chars. Edge labels ≤ 40 chars. If a message needs
more detail, add a `Note over X: …` line. Users open diagrams in a
modal to see them full-size, but they still need to scan quickly.

## 6. SUBGRAPH GROUPING FOR HLD

In HLD flowcharts, group nodes into subgraphs that mirror real repo
boundaries:

    subgraph OS[OrderService]
        OrderPlacement
        ItemChecker
    end
    subgraph FF[FulfillmentService]
        StockCheckHandler
        Cache
    end

This tells the reader "this work happens inside repo X" at a glance.

## 7. DIAGRAM TYPE CHEAT-SHEET

| Question shape                          | Diagram type          |
| --------------------------------------- | --------------------- |
| "How does <system> work?" (≥2 services) | flowchart TB (HLD)    |
| "What's the flow for <specific call>?"  | sequenceDiagram (LLD) |
| "What states can <X> be in?"            | stateDiagram-v2 (LLD) |
| "What are the components of <X>?"       | flowchart LR (HLD)    |
| "How does data move through <pipeline>?"| flowchart LR (HLD)    |
| Decision trees / routing logic          | flowchart TD (LLD/HLD)|

## 8. EXAMPLE — HLD

```mermaid
%% HLD: Stock checking — service orchestration
flowchart LR
    Client([Web/Mobile Client]):::human
    subgraph OS[OrderService]
        OrderPlacement[OrderSubmitHandler]
        ItemVal[ItemChecker]
    end
    subgraph FF[FulfillmentService]
        StockVal[StockCheckHandler]
        Cache[(StockCheckCache<br/>2h TTL)]:::datastore
    end
    subgraph SG[stock-gateway]
        StockCtl[StockLookupHandler]
    end
    Supplier[Supplier API]:::external

    Client -->|POST /orders/submit| OrderPlacement
    OrderPlacement -->|schema validation| ItemVal
    OrderPlacement -->|POST /stock_check/items| StockVal
    StockVal -->|cache lookup| Cache
    StockVal -->|lookupStock| StockCtl
    StockCtl -->|GET /levels| Supplier

    classDef service fill:#1e3a5f,stroke:#3b82f6,stroke-width:1.5px,color:#e5e7eb
    classDef external fill:#374151,stroke:#9ca3af,stroke-width:1px,color:#e5e7eb,stroke-dasharray:4 3
    classDef datastore fill:#422006,stroke:#d97706,stroke-width:1px,color:#fef3c7
    classDef human fill:#064e3b,stroke:#10b981,stroke-width:1px,color:#d1fae5
    class OrderPlacement,ItemVal,StockVal,StockCtl service
```

## 9. EXAMPLE — LLD sequence

```mermaid
%% LLD: OrderService stock check sequence
sequenceDiagram
    participant Client
    participant OS as OrderService
    participant FF as FulfillmentService
    participant SG as stock-gateway

    Client->>OS: POST /orders/submit
    activate OS
    Note over OS: Request body schema validation
    OS->>OS: ItemChecker.validate()
    OS->>FF: POST /stock_check/items
    activate FF
    Note right of FF: StockCheckCache (2h TTL)<br/>short-circuit if supplier down
    FF->>SG: lookupStock()
    SG-->>FF: IN_STOCK | OUT_OF_STOCK | UNKNOWN
    FF-->>OS: StockCheckResponse
    deactivate FF
    OS->>OS: OrderStateMachine.process()
    OS-->>Client: 200 OK | 409 | 422
    deactivate OS
```

## 9b. FAILURE-PROOFING — common syntax errors that break the renderer

The mermaid parser (v11+) is strict; LLM-generated diagrams regularly
trip on these specific patterns. AVOID them:

* **Unquoted special characters in node labels.** Parens, brackets,
  slashes, ampersands, colons, and HTML entities inside `[...]` break
  the parser. WRAP THE LABEL IN DOUBLE QUOTES.

      Bad:   OrderSvc[Order Service (beta)]
      Bad:   Doc[GET /api/v1/foo]
      Bad:   Cache[Redis & DynamoDB]
      Good:  OrderSvc["Order Service (beta)"]
      Good:  Doc["GET /api/v1/foo"]
      Good:  Cache["Redis & DynamoDB"]

* **Reserved keywords as node IDs.** Mermaid reserves: `end`, `class`,
  `subgraph`, `style`, `linkStyle`, `direction`, `participant`,
  `state`. Pick a different identifier.

      Bad:   end[End State]               # 'end' is reserved
      Good:  finishNode["End State"]

* **Hyphens / dots / slashes in unquoted node IDs.**

      Bad:   carrier-gateway -->|x| svc
      Good:  carrierGateway -->|x| svc
      OR:    CG[carrier-gateway] -->|x| svc      # quote in label only

* **Mixed diagram syntax.** Don't use `->>` (sequence) inside a
  `flowchart` block, and don't use `-->` (flowchart) inside a
  `sequenceDiagram` block. One block, one diagram type.

* **Empty subgraphs.** `subgraph X[Title]\nend` with no nodes inside
  errors out. Either populate the subgraph or remove it.

* **Class assignments to non-existent nodes.** Every node named in a
  `class A,B,C svc` line must have been declared above.

* **HTML in labels.** Mermaid supports `<br/>` only — no `<b>`, no
  `<span>`, no `<div>`. For emphasis, use markdown bold inside the
  quoted label: `Foo["**Critical** path"]`.

* **Trailing commas in class lists.** `class A,B, service` with a
  trailing comma errors. Strip them.

* **Forgotten `direction` line on `stateDiagram-v2`.** State diagrams
  default to TB; if you want LR, declare it: `direction LR` after
  `stateDiagram-v2`.

When in doubt: keep node IDs alphanumeric (camelCase or snake_case),
ALWAYS quote labels, and emit one diagram type per fenced block.
A "didn't ship a diagram" beats "shipped a broken diagram" every
time — the renderer's failure state interrupts the answer flow.

## 10. EXAMPLE — LLD state diagram

```mermaid
%% LLD: Order state transitions
stateDiagram-v2
    [*] --> PENDING
    PENDING --> STOCK_VALIDATION: submit
    STOCK_VALIDATION --> CONFIRMED: stock available
    STOCK_VALIDATION --> BACKORDERED: partial stock
    STOCK_VALIDATION --> DECLINED: out of stock
    CONFIRMED --> PACKING: items allocated
    PACKING --> COMPLETE: parcel shipped
    PACKING --> CANCELLING: user cancel
    CANCELLING --> CANCELLED: stock released
    BACKORDERED --> CONFIRMED: stock restored
    BACKORDERED --> DECLINED: backorder expired
    COMPLETE --> [*]
    DECLINED --> [*]
    CANCELLED --> [*]
```

# ─────────────────────────────────────────────────────────────────
# END OF DIAGRAM STANDARDS
# ─────────────────────────────────────────────────────────────────
"""
"""Injected into every prompt that may produce a Mermaid diagram.

Keep this block internally consistent — the UI's renderer + modal
viewer is tuned to these shapes (header `%% HLD:` / `%% LLD:`
directive surfaced as the label, classDef colours matching the dark
theme, etc.). If you change styling here, update
``ui/src/components/mermaid.tsx`` at the same time.
"""
