# Bifrost research and experiment roadmap

**Companion to:** [prd.md](archive/prd.md) (PRD), [trd.md](archive/trd.md) (TRD), [handbook/](handbook/README.md)
**Status:** research and plan only. No experiment in this document has run yet. Decisions recorded on 2026-11-05.

> **TL;DR.** Teams building similar platforms index the same kinds of sources bifrost already reads. They differ in how they extract structure, retrieve, verify and measure. This roadmap starts with measurement, because the current benchmark cannot yet tell a real gain from noise. It holds 25 experiment cards in six phases, plus a standing security gate before any multi-user deployment.

> **Read this when** a change touches retrieval, verification, planning or the benchmark, or the question is why the next experiments run in this order.
> **Skip if** the question is how the system works today. The [handbook](handbook/README.md) covers that.

---

## 1. Verdict

**Do not add sources yet. Fix measurement first, then verification, planning and retrieval.** Among the 20 systems surveyed, only Unblocked documents more source categories than bifrost's six of eight. Where bifrost differs from the evidence is on three axes, and its benchmark cannot tell whether a change on any of them helps.

- **Structure.** bifrost extracts structure with an LLM only. Leaders extract it with parsers such as SCIP, tree-sitter and static analysis, and use the LLM to explain what the parser found.
- **Retrieval.** bifrost retrieves with generic MiniLM vectors and has no lexical index outside handover docs. Leaders use hybrid lexical and dense retrieval, or grep-first agents over a lexical index.
- **Verification.** bifrost runs one LLM adversarial verifier, which times out on about 30% of benchmark turns [ran]. Leaders run deterministic checks first, then cheap checkers, and send only disputed claims to an LLM.
- **Measurement.** The benchmark has 12 questions and a regex scorer. A full A/B/C run cost about $44-51 in model usage in late 2026. The three baseline run means were 0.734, 0.741 and 0.738, so the 0.007 spread between identical runs is as large as the +0.006 that a later experiment run reported over the baseline mean [ran].

So measurement comes first. Phase 0 re-scores runs that already exist, so it needs almost no new benchmark spend.

---

## 2. Decided plan

**Ship the planner floor now, then measure, then fix the known failures, then test retrieval on a DeepCodeBench subset, and build the unified index only after that.** This order follows from the four decisions taken on 2026-11-05, which [section 13](#13-decisions-taken-2026-11-05) records. [Section 8.4](#84-phase-sequence-and-gates) lists the same order with each phase's entry gate.

1. **Now, the planner floor (P1.1).** It ships ahead of Phase 0 under the existing regression rule. It is exempt from the measure-first gate because it fixes a bug the baseline runs already show, where the planner dispatched no agents on q3, q4, q5 and q12. Its first check is a count of zero-dispatch turns, and a count needs no judge. Its recall gain on those four questions is read against the E0.1 noise floor once that is published. It needs one A-only run, estimated at about $15-17.
2. **Phase 0, measurement, before the rest of Phase 1.** E0.1 (noise floor), E0.2 (calibrated judge) and E0.3 (citation existence and confidence calibration) run first, then E0.4 (abstention set and closed-book control). The small local claim checker is set up here for E0.3's support check, and P1.2 reuses it. Phase 1 starts once E0.1-E0.3 are done. That means a published noise floor, a judge at ρ ≥ 0.6 against human labels, and the anchor check that P1.3 reads. P1.4 also waits for E0.4, because its metric is the E0.4 abstention set. If E0.2 hits its kill criterion, Phase 1 goes ahead on the regex scorer with human spot checks, as the E0.2 card says.
3. **Phase 0b, external truth, beside Phase 1.** E0.5 takes a 30-50 question subset of DeepCodeBench from its 1-2 smallest repos. DeepCodeBench questions come with fact lists that the E0.2 judge can score. Taking the smallest repos aims at the low end of the $30-300 extraction estimate, which a pilot has not yet confirmed. E0.5 also adds a renamed-symbol run, so answers the model memorised fail. E0.6 builds blast-radius truth from the hand-written AcmeCo seeds. Phase 0b starts once the Phase 0 judge exists, and it gates Phase 2, not Phase 1.
4. **Phase 1, the failures already visible on AcmeCo.** P1.2 checks claims one at a time. Deterministic checks go first, then the small local checker, and only the claims it disputes reach the LLM verifier. P1.3 runs the specificity diagnostic, then the anchor rule or localisation. P1.4 adds the sufficient-context gate after E0.4. P1.5, the structured evidence handoff, comes last in the phase. Full-text search over Confluence, Jira and Google Docs pages ships with this phase as well.
5. **Phase 2, retrieval and structure, on the DeepCodeBench subset.** It starts once Phase 0b has registered the subset with its own noise floor. P2.1, the retrieval matrix, runs first. Two of its five configs use small local models on CPU, one a reranker and one a code embedder. P2.2 to P2.5 follow in card order.
6. **Phase 3, design bets, after the P2.1 matrix.** P3.1, one hybrid index with one search tool, starts only after P2.1 and reuses the hybrid index P2.1 builds. It also needs benchmark questions that use non-code sources, seeded first. P3.2, the conflict policy, and P3.3, the PR "why" lineage, follow.
7. **Phases 4 and 5, after Phase 3.** Both need the AcmeCo seeds and questions extended first. Phase 4 adds new sources in three cards. P4.1 joins CI and deploys to code by commit SHA, P4.2 retrieves similar past incidents weighted by recency, and P4.3 links feature-flag keys to the code that reads them. Phase 5 covers learning and trust. P5.1 turns feedback into eval scenarios, P5.2 stores bitemporal facts and keeps raw pages, and P5.3 checks an activity-based expert graph against CODEOWNERS.
8. **Before more than one user, the standing gate.** bifrost has to mirror source permissions and treat ingested Slack, Jira and PR text as untrusted before it serves more than one user. This gates deployment, not any one phase.

Two prerequisites are still open, labelling time for E0.2 and model-usage spend during Phase 0. [Section 12](#12-open-prerequisites) lists both.

---

## 3. Summary of findings

- Almost every system indexes code, and most add PR and commit history. Enterprise-knowledge products add docs, tickets and chat. Incident tools add telemetry and deploys.
- The logic the leaders share is parsers for structure, lexical plus dense retrieval, deterministic checks before LLM checks, and fact-level judges over large question sets.
- The roadmap has 25 experiment cards in six phases, with Phase 0b split out of Phase 0. The first three are measurement and cost almost nothing, because they re-score runs bifrost already has.

---

## 4. Method and limits

- The survey ran four web research slices. They covered about 25 commercial products, in-house builds at about 15 companies, open-source tools and papers, and organisational knowledge sources and trust.
- The bifrost capability map comes from a read-only scan of the code at commit 660d5cc. Benchmark numbers come from three baseline runs and two later experiment runs on the AcmeCo set.
- Amazon publishes nothing usable on this. LinkedIn, Pinterest and Booking had no usable write-ups beyond one LinkedIn paper.
- Netflix's Pensive post and a DoorDash post returned 403. Grab's Kinabalu post returned 404, an Atlassian Code Context page returned 403 and a Tabnine page returned 404. Pensive detail comes from secondary sources.
- The survey did not read Spotify's slide PDF or bifrost's `src/bifrost/orchestration/skeptic.py`, and it ran no experiment.

Source tags used below:

- `[primary]` is a paper or a team's own write-up of an in-house system.
- `[vendor]` is a company describing its own product. Numbers with this tag are vendor-reported.
- `[secondary]` is someone else's report.
- `[read]` marks a bifrost fact taken from the code scan.
- `[ran]` marks a bifrost fact taken from benchmark runs.

Every field number is reported by its source and has not been reproduced here.

---

## 5. What the field builds

### 5.1 Data sources by product family

**Code and PR history are universal. Chat, CI and ownership data are rare.**

| Family | Examples | Sources they index | Logic that stands out |
|---|---|---|---|
| Code search and assistants | Sourcegraph, Cursor, Copilot, Augment, Greptile, Rovo, Bito | Code, and some add PR and commit history (Augment, Greptile, Copilot) | Trigram or lexical index plus a symbol graph built with SCIP or tree-sitter. Agentic grep is replacing embeddings as the main leg. |
| Review bots | CodeRabbit, Qodo, Greptile review | Code, the PR diff, linked tickets | Per-PR code graph, verifier agents, learnings mined from reviewer replies. |
| Enterprise knowledge | Glean, Unblocked, Dropbox Dash | Code, PRs, docs, tickets, chat. Unblocked adds CI and Datadog or Sentry. | Pre-built hybrid index with permissions per item or edge, conflict resolution (Unblocked), one search tool (Dash). |
| Docs generators | DeepWiki, Swimm, Moderne, Google Code Wiki | Code, with a PR check for drift (Swimm) | Static analysis or file clustering first, then the LLM writes prose. |
| In-house assistants | Uber Genie and EAg-RAG, Spotify AiKA | Docs, Slack channels, the Backstage catalog | Enriched chunks, BM25 plus vectors, golden sets written by subject experts. |
| Incident and RCA tools | Meta RCA and DrP, RCACopilot, Datadog Bits, Sentry Seer | Code changes, incidents, telemetry, ownership | Ownership and the code graph narrow the candidates, then a ranker orders them. Similar past incidents are retrieved by recency. |
| bifrost | this repo | Code, PRs (live agent), Jira, Confluence, Google Docs, Slack (live agent), Backstage and CODEOWNERS | LLM-only extraction, MiniLM vectors, a sparse graph, one agent per source. |

### 5.2 Source categories by how many systems ingest them

| Source category | Systems out of 20 that ingest it | bifrost ingests it |
|---|---|---|
| Code | 16 | yes |
| PR and commit history | 12 | yes |
| Docs and wikis | 10 | yes |
| Tickets | 7 | yes |
| Incidents and telemetry | 5 | no |
| Chat | 3 | yes |
| CI and deploys | 2 | no |
| Ownership catalog | 2 | yes |

The counts come from the docs and posts in the research slices. A source the docs do not mention counts as absent, so the low counts are lower bounds.

### 5.3 Retrieval, structure, verification and evaluation

**The leaders differ from the rest in verification and evaluation, not in sources.**

#### What everyone does

- They combine lexical and dense retrieval, or run a grep-first agent over a lexical index. Sourcegraph dropped ada-002 embeddings for search-based retrieval [vendor], Glean runs lexical OpenSearch with camelCase tokens beside code embeddings [vendor], and Uber's EAg-RAG uses BM25 plus vectors [primary].
- Structure comes from parsers, and the LLM explains it. Sourcegraph uses SCIP, Greptile and Aider use tree-sitter, and Swimm and Moderne run static analysis first [vendor].
- Chunks follow the syntax tree, and many index an LLM summary next to the code. Greptile reports cosine 0.815 for docstring embeddings against 0.728 for raw code, a vendor-reported number [vendor].
- Retrieval runs as an isolated subagent that returns a summary. Examples are Sourcegraph Deep Search, Amp's Search subagent and Cognition's SWE-grep, which Cognition says runs 8 parallel greps per turn [vendor].
- Freshness is event-driven. Cursor's Merkle-tree sync cut time to first query from 7.87s to 525ms at p50, a vendor-reported number [vendor].
- Everyone cites sources, and no one publishes calibrated confidence. Atlassian's Rovo docs say its semantic results carry no confidence score [vendor].
- Evals come in three kinds: PR-derived fact recall (Qodo DeepCodeBench), seeded bugs (Greptile) and online A/B tests (Cursor, Copilot). Independent boards rate the vendors lower than their own benchmarks do.

#### What only leaders do

- They verify memory against code before using it. Copilot Memory stores file:line citations, checks them against the current branch and expires them after 28 days. Its A/B test showed a PR merge rate of 90% against 83%, a vendor-reported number [vendor].
- They run deterministic and cheap checks before LLM judges. Uber's uReview generates, grades, thresholds, dedupes and suppresses whole categories, and 65% of its comments are addressed against 51% for human comments [primary].
- They found the expensive LLM judge adds little. Atlassian's RovoDev reviewer found its LLM factual judge had minimal effect while being the costliest stage, and an actionability filter added 15-20 points [primary].
- They state who wins a conflict. Unblocked puts code first, then a recent expert statement, then docs as statements of intent, and shows the conflicts it cannot resolve [vendor].
- They draw typed edges with provenance and permissions. Glean's graph carries ACL, confidence and provenance on every edge [vendor], and Bito draws typed cross-repo edges [vendor].
- They build large, calibrated eval sets. Uber's 100+ question expert golden set with a judge measured +27% relative acceptable answers and −60% relative incorrect advice [primary]. Dropbox reports tracking judge disagreement, 8% at first, and NDCG [vendor].

#### What was tried and abandoned

- A graph database at query time. Dropbox dropped it and flattened its graphs into knowledge bundles [vendor].
- Many tools and federated live APIs. Vercel cut 17 tools to 2 and reports success going from 80% to 100% at 3.5x the speed, vendor-reported numbers [vendor]. Dropbox replaced 5-10 tools with one after its MCP path took up to 45s [vendor]. Stripe hands agents curated subsets of about 500 tools [primary].
- An expensive LLM fact judge on every output. RovoDev found it did little, as described above [primary].
- Generated context files prepended to every prompt. ETH found AGENTS.md-style files gave no success gain at 20% more cost [primary]. Meta's compass files helped when agents retrieved them, as [section 7](#7-where-the-evidence-disagrees) describes [primary].
- Generic embeddings as the main retrieval leg. Sourcegraph dropped ada-002 [vendor], Continue deprecated `@Codebase` [primary], and Cursor's docs say it "does not store embeddings of your codebase" [vendor].

---

## 6. Where bifrost stands

**bifrost's source coverage is already broad. Its gaps are in structure, retrieval, verification and measurement.**

### 6.1 Current state

- Code is always on, and Jira and Google Docs agents run by default. The Slack, GitHub and Confluence agents are opt-in, because they cited a source on only about 0-5% of turns [read].
- Extraction is LLM-only across pipeline phases A to I. There is no tree-sitter, AST or SCIP layer [read].
- Embeddings are MiniLM-L6-v2 at 384 dimensions with a 128-token cap. Full-text search exists only on handover docs [read].
- The graph has about 0.12 edges per entity, and cross-repo edges come from name matching. Both facts come from the docstring in `src/bifrost/research/graph_retrieval.py` [read].
- The benchmark has 12 questions over a fictional 5-repo AcmeCo, scored by regex and substring match. Specificity counts file:line, Jira and PR references without checking that they exist [read].

### 6.2 The question pipeline and its gaps

Stages come from the code scan [read]. Gap numbers come from the baseline benchmark runs on the 12 AcmeCo questions [ran].

| Order | Stage | Gap | Largest measured gap |
|---|---|---|---|
| 1 | Question | | |
| 2 | Scope and fast path | Regex what-is and who-owns questions answer here with zero agents dispatched. | |
| 3 | Orchestrator and planner | Dispatched no agents on q3, q4, q5 and q12 in the baseline runs. | yes |
| 4 | Router | Seeds from MiniLM with its 128-token cap, entity names and node degree. There is no lexical index or AST, and the graph has 0.12 edges per entity. | |
| 5 | Per-source agents | The code agent greps the clone. The Slack, GitHub and Confluence agents are off by default and cited on about 0-5% of turns. | |
| 6 | Synthesis | Specificity is 0.35 and precision is 0.63. q12 never says "could not determine". | |
| 7 | Adversarial verifier | Times out on about 30% of benchmark turns. | yes |

One gap sits outside this pipeline. The blast-radius benchmark derives its ground truth from the same graph it walks, so it measures how the walk behaves, not which services a change would really hit [read].

### 6.3 Pushback on the per-source fan-out

**Adding more per-source agents is not the next step. The agents bifrost has beyond code are rarely cited, and the teams that ran many live tools cut them.**

- bifrost's Slack, GitHub and Confluence agents cited a source on only about 0-5% of turns, which is why they are opt-in [read].
- Coverage is not where bifrost trails. Of the 20 systems surveyed, only Unblocked documents more source categories than bifrost's six of eight.
- More agents mean more planner-to-agent handoffs, and the handoff is already where bifrost fails. Its planner dispatched no agents on 4 of the 12 questions [ran], and SWE-QA traced 41.8% of agentic failures to the planner-to-subagent handoff [primary].
- Teams with many tools or live APIs cut them. Vercel cut 17 tools to 2 and reports success going from 80% to 100% at 3.5x the speed, vendor-reported numbers [vendor]. Dropbox replaced 5-10 tools with one after its MCP path took up to 45s [vendor].
- SWE-QA measured an index-based system at 65.2% against 46.2% for an agentic one, at under half the cost per correct answer [primary]. The evidence on index against grep is split, as [section 7](#7-where-the-evidence-disagrees) shows, so this is a reason to test, not a result.
- The alternative is tested rather than assumed. P3.1 builds one hybrid index with one search tool after P2.1, and keeps the per-source agents as fallbacks. If the non-code citation rate does not move, the per-source agents stay.

---

## 7. Where the evidence disagrees

- **Grep against embeddings.** For embeddings, Cursor reports +12.5% accuracy from semantic search on its own Context Bench, a vendor-reported number [vendor], and the SWE-QA study measured an index-based system at 65.2% against 46.2% for an agentic one, at under half the cost per correct answer [primary]. Against them, Claude Code ships grep-first with no index, and a secondary write-up reports the reasoning as "just vibes" rather than a benchmark [secondary]. A separate study found keyword-only agents reach over 90% of RAG quality [primary]. AcmeCo is too small to settle this, so P2.1 runs it on the external set.
- **Whether Cursor removed its embedding index.** Cursor's mid-2026 docs say it "does not store embeddings of your codebase" [vendor]. The only article describing a removal, on dbggr.dev, is unverified [secondary], and a forum reply gives the reason [secondary]. This roadmap treats the removal as unsettled.
- **Context files.** Meta reports about 40% fewer tool calls from its 59 compass files, a preliminary result over 6 tasks [primary]. ETH found AGENTS.md-style files gave no success gain and cost 20% more [primary]. The two differ in how the files are used. Meta's agents retrieve them on demand, and ETH prepends them to every prompt. P2.5 tests the retrieved form.
- **LLM fact judges.** RovoDev found its LLM factual judge had minimal effect [primary]. CoVe's factored verification doubled list precision from 0.17 to 0.36 [primary]. CoVe checks independent atomic questions, while RovoDev's judge scored whole comments, so P1.2 follows CoVe's shape.
- **Graph RAG wins.** LightRAG reported a 66.7% win rate that fell to 39.1% when another team re-ran it with unbiased judging [primary]. Any judge-measured gain in this roadmap therefore needs swapped answer order and human labels, which is why E0.2 has both.

---

## 8. The ranked roadmap

**Measure first, then fix the failures already visible on AcmeCo, then test retrieval and structure on an external question set.**

Each card below carries a hypothesis, the change, a metric with a target and a kill criterion, the evidence, effort as a human baseline next to an AI-assisted estimate, and benchmark cost. Effort is anchored on spec, review, labelling and benchmark time, because with an agent the code is the cheap part. Benchmark cost is model usage. Figures marked as estimates have not been measured.

### 8.1 Value against cost

Value means expected impact times strength of evidence. Cost means effort plus benchmark spend. Rank 1 is the highest value or the lowest cost. Placement is a judgement from each card's effort, spend and evidence, not a measurement. Only the twelve items below were placed, and none fell in the cheap but lower-value group.

| Item | Value rank | Cost rank | Placement |
|---|---|---|---|
| E0.1 | 2 | 1 | Do first. Start here. |
| E0.2 | 1 | 5 | Do first |
| P1.2 | 3 | 7 | Do first |
| E0.3 | 4 | 4 | Do first |
| P1.1 | 6 | 2 | Do first |
| P1.4 | 8 | 6 | Do first |
| P1.3 | 9 | 3 | Do first |
| Phase 0b (E0.5, E0.6) | 5 | 9 | Plan for it |
| P1.5 | 7 | 8 | Plan for it |
| P2.1 | 10 | 10 | Later |
| P2.3 | 11 | 11 | Later |
| P3.1 | 12 | 12 | Later |

### 8.2 Why the order looks like this

- Hybrid lexical retrieval sits in Phase 2, merged with the embedding ablation into one retrieval matrix (P2.1). The code agents already grep the clones, so code already has a lexical leg. Long context matches RAG on small repos [primary], so AcmeCo is unlikely to show a retrieval gain. One matrix of runs also costs less than two separate ablations.
- One cheap piece of it ships early. Full-text search over Confluence, Jira and Google Docs pages avoids MiniLM's 128-token truncation of long pages. It ships with Phase 1 once the Phase 0 gate passes, because decision D1 chose measure first.
- The external set (E0.5) and the non-circular blast truth (E0.6) form a Phase 0b that gates Phase 2, not Phase 1. Phase 1 fixes failures already visible on AcmeCo and measurable with the new judge, while external repos need extraction spend first.
- The specificity diagnostic sits at the top of Phase 1 (P1.3) as the gate between an anchor rule and localisation. It is free from existing data. Because the scorer counts anchors without checking them, an anchor rule without the E0.3 existence check could raise the score with made-up references.
- The planner floor (P1.1) ships now under the existing regression rule. It is a bug fix on 4 of the 12 questions and the cheapest item on the list.
- The structured evidence handoff (P1.5) is its own card after the floor. It refactors the mature 6,300-line coordinator in `src/bifrost/research/coordinator.py`, which is where AI help shrinks effort the least.
- Typed cross-service edges (P2.2) come ahead of the tree-sitter layer. Blast radius is the question bifrost answers that most others do not, and `src/bifrost/manifest/env_miner.py` already mines the host, URL, topic and queue keys. Neither has published accuracy evidence.
- Permission mirroring and untrusted ingested text sit outside the ranking, as a standing gate before any multi-user deployment. Research agents read Slack, Jira and PR text while holding tools, and the task executor in `src/bifrost/actions/executor.py` runs with `bypassPermissions`.
- E0.3 includes calibration of the headline confidence. No surveyed system publishes calibrated confidence, and bifrost already emits confirmed, inferred or uncertain on every answer.
- P5.2 includes raw-page retention. `src/bifrost/research/external_indexer.py` distils Confluence pages with Sonnet before embedding them, and one memory study found verbatim retrieval beat extracted facts 67.4% to 45.4% [primary].

### 8.3 Top ten

1. E0.1 · Noise floor and minimum detectable effect from the 5 existing runs. Free.
2. E0.2 · A calibrated fact-level judge that re-scores the existing runs.
3. E0.3 · Citation existence check, ALCE citation metrics and confidence calibration.
4. P1.1 · Planner hard floor, shipped now as a bug fix.
5. P1.2 · Deterministic-first factored verification with per-claim timeouts and a small local checker.
6. P1.3 · Specificity diagnostic, then the anchor rule or localisation.
7. E0.4 with P1.4 · An abstention set and a sufficient-context gate that names the code owner.
8. P1.5 · Structured evidence handoff from agents to synthesis.
9. Phase 0b · A DeepCodeBench question subset, a renamed-symbol run and non-circular blast truth.
10. P2.1 · Retrieval matrix on the DeepCodeBench subset, including a local reranker and a local code embedder.

### 8.4 Phase sequence and gates

Each phase starts only when its entry gate passes and the phases before it have finished.

1. **Phase 0, measure.** Entry gate is none. It re-scores old runs. Start here.
2. **Phase 0b, external truth.** Entry gate is the Phase 0 judge. It runs beside Phase 1.
3. **Phase 1, known failures.** Entry gate is a judge at ρ ≥ 0.6 and a published noise floor. It follows Phase 0.
4. **Phase 2, retrieval.** Entry gate is the external set registered in Phase 0b. Both Phase 1 and Phase 0b finish before it.
5. **Phase 3, design bets.** Entry gate is benchmark questions that use non-code sources, seeded first. It follows Phase 2.
6. **Phases 4 and 5, sources and trust.** Entry gate is the AcmeCo seeds extended first. They follow Phase 3.

Three rules sit on top of this order. The planner floor (P1.1) ships before Phase 1 as a bug fix. P3.1 starts only after the P2.1 matrix, per decision D4. The standing security gate applies to every phase before a multi-user deployment.

### 8.5 Phase 0, measurement

#### E0.1 · Noise floor and minimum detectable effect

| Field | Detail |
|---|---|
| Hypothesis | The run-to-run spread of the composite score is as large as the gains recent experiment runs reported. |
| Change | Compute per-question and per-run variance from the 5 existing runs, three baseline runs and two later experiment runs, paired by question. Bootstrap the smallest effect that 80% power can detect. |
| Metric | The minimum detectable effect on the composite and on each component. |
| Target | A published number every later card uses as its bar. |
| Kill criterion | This is a measurement, so there is no kill. If the effect is above 0.02, the gains the two later runs reported over the baseline mean, +0.006 and +0.002, read as noise. |
| Evidence | Datadog found its full eval set caught a regression a subset missed [vendor]. A 2025 paper argues for bias-corrected reporting of judge scores [primary]. Grab: "Vibe check is not an evaluation strategy" [primary]. |
| Effort | Human baseline 1 day. AI-assisted 0.5 day. |
| Benchmark cost | $0. |

#### E0.2 · Calibrated fact-level judge

| Field | Detail |
|---|---|
| Hypothesis | A fact-level LLM judge agrees with a human labeller well enough to detect changes the regex scorer cannot. |
| Change | Split each question's `expected_key_facts` into atomic facts. Judge each answer against each fact with the answer order swapped and the median of 3 samples. Hand-label about 50 answer-fact pairs, then re-score the 5 existing exported runs. |
| Metric | Spearman correlation between judge and human labels. |
| Target | ρ ≥ 0.6. RovoDev reported 0.69 on 47 labels. |
| Kill criterion | ρ stays under 0.5 after one prompt revision. Then keep the regex scorer and add human spot checks instead. |
| Evidence | Qodo's DeepCodeBench scores answers by LLM fact recall [vendor]. AutoNuggetizer reached Kendall tau of about 0.89 against manual fact lists [primary]. ARES calibrates a judge with about 150 human labels [primary]. LightRAG's 66.7% to 39.1% drop shows why order must be swapped [primary]. |
| Effort | Human baseline 6-8 days. AI-assisted 3 days, of which about 1 day is human labelling, which does not shrink. |
| Benchmark cost | No new benchmark runs. Judge calls are estimated at $10-25 for 5 runs. |

#### E0.3 · Citation existence, ALCE metrics and confidence calibration

| Field | Detail |
|---|---|
| Hypothesis | Some file:line anchors that raise the specificity score today do not exist at the indexed commit, and the headline confidence is not calibrated. |
| Change | Check deterministically that each cited path and line range exists at the indexed SHA. Add ALCE citation precision and recall, which ask whether the cited span supports the claim. The small local claim checker scores that support. Compare confirmed, inferred and uncertain headlines against judged correctness. |
| Metric | Share of anchors that resolve, ALCE precision and recall, and correctness rate per confidence level. |
| Target | At least 95% of anchors resolve, and at least 90% of "confirmed" answers are judged correct. |
| Kill criterion | This is a measurement. If under 80% of anchors resolve, specificity leaves the composite until synthesis is fixed. |
| Evidence | ALCE's automatic citation metrics agreed with humans 85% and 78% of the time [primary]. Copilot Memory checks file:line citations against the branch before using them [vendor]. DOCER found stale code references in 28.9% of repos it scanned [primary]. |
| Effort | Human baseline 3-4 days. AI-assisted 1.5 days. |
| Benchmark cost | $0 for existence. The support check runs on the small local checker that decision D3 approved, so it has no API spend. Through `claude -p` it would cost about $5, an estimate. |

#### E0.4 · Abstention set and closed-book control

| Field | Detail |
|---|---|
| Hypothesis | bifrost answers questions its sources cannot support, and some correct answers come from model priors rather than retrieval. |
| Change | Add about 8 unanswerable questions, built from symbols and facts that are not in any seed, in a separate question file, because [`acme.yml`](benchmarks/acme.yml) is frozen. Run the 12 questions once with retrieval off. |
| Metric | Abstention rate on the unanswerable set, and the score gap between retrieval on and off. |
| Target | At least 80% of unanswerable questions get "could not determine". |
| Kill criterion | This is a measurement. A small closed-book gap means the benchmark mostly measures the model, not bifrost. |
| Evidence | Google's sufficient-context work raised selective accuracy by 2-10 points [primary]. Code-QA-Bench's closed-book control found code added +0.23 and docs +0.07 [primary]. Meta's RCA withholds low-confidence answers [primary]. |
| Effort | Human baseline 3 days. AI-assisted 1.5 days. |
| Benchmark cost | Estimated at about $15-20 for an A-only run of the new file and about $15 for the closed-book run. |

### 8.6 Phase 0b, external truth on a DeepCodeBench subset

#### E0.5 · External question set and renamed-symbol run

| Field | Detail |
|---|---|
| Hypothesis | AcmeCo is too small and too regular to show retrieval or structure gains, and an external set will show them. |
| Change | Take a 30-50 question subset of DeepCodeBench from its 1-2 smallest repos, per decision D2. Its questions come with fact lists, so the E0.2 judge can score them. Add a run kind that renames key symbols in the clones, so memorised answers fail. |
| Metric | Judge score on the subset, with its own noise floor from the E0.1 method. |
| Target | A registered set with a known noise floor. This is the gate into Phase 2. |
| Kill criterion | Extraction of the chosen repos costs more than the spend planned for Phase 0b. Then keep only the smallest of them. |
| Evidence | DeepCodeBench has 1,144 PR-derived questions over 8 repos, with vendor-reported scores of 76-80 for Qodo and 64 for Claude Code [vendor]. SWE-QA's best system scored about 47.8 [primary]. OpenHands scored 43.2% on SWE-bench Verified and 19.25% on SWE-bench-Live, which shows how much a familiar set inflates scores [primary]. |
| Effort | Human baseline 8-10 days. AI-assisted 4 days. |
| Benchmark cost | Extraction is estimated at $30-300 depending on repo size, from the handbook's $10-30 per 200 files in [06, extraction pipeline](handbook/06-extraction-pipeline.md). That figure covers Phase D alone, so the other phases push the low end up. Taking DeepCodeBench's smallest repos aims at the low end, which a pilot has not confirmed. Each A-only run adds an estimated $15-20. |

#### E0.6 · Non-circular blast-radius truth

| Field | Detail |
|---|---|
| Hypothesis | The blast-radius benchmark measures the graph walk, not real impact, because its truth comes from the same graph. |
| Change | Derive the truth from the hand-written AcmeCo seed sources in [`examples/acmeco`](../examples/acmeco/README.md), which do not depend on extraction. |
| Metric | Blast-radius precision and recall against the seeded truth. |
| Target | A baseline that P2.2 and P2.3 must beat. |
| Kill criterion | This is a measurement. |
| Evidence | bifrost's own `src/bifrost/benchmarks/blast_radius_benchmark.py` derives truth from the walked graph [read]. |
| Effort | Human baseline 2 days. AI-assisted 1 day. |
| Benchmark cost | $0. |

### 8.7 Phase 1, planning, verification and abstention

These start once E0.1-E0.3 are done, per decision D1. P1.1 is the exception and ships now, and P1.4 also waits for E0.4.

#### P1.1 · Planner hard floor

| Field | Detail |
|---|---|
| Hypothesis | q3, q4, q5 and q12 lose recall because nothing is dispatched, not because agents fail. |
| Change | First check from `agent_results` whether the fast path or the planner skipped dispatch. Then require at least one code agent on the top-ranked repo for any question that leaves the fast path. |
| Metric | Zero-dispatch turns, and recall on the four questions. |
| Target | No zero-dispatch turns, and recall on the four questions up by more than the noise floor with no composite regression. |
| Kill criterion | Recall on the four does not move. Then the router, not the planner, is the cause. |
| Evidence | In CodeCompass, 58% of trials made zero graph calls even with the tool available [primary]. SWE-QA traced 41.8% of agentic failures to the planner-to-subagent handoff [primary]. Anthropic's research system puts explicit effort rules in the lead agent's prompt [vendor]. |
| Effort | Human baseline 1-2 days. AI-assisted 0.5 day. It ships now under the existing regression rule. |
| Benchmark cost | One A-only run, estimated at about $15-17. |

#### P1.2 · Deterministic-first factored verification

| Field | Detail |
|---|---|
| Hypothesis | Checking claims one at a time, with deterministic checks first, removes the timeouts and catches more wrong claims. |
| Change | Extract claims, then check path existence, symbol existence at the SHA and quoted-span match without an LLM. Give each claim its own timeout. Pass the claims those checks cannot settle to the small local claim checker from E0.3, and send only the claims it disputes to the LLM verifier. |
| Metric | Verifier completion rate, precision and latency. |
| Target | The verifier completes on at least 98% of turns, and precision rises by more than the noise floor at equal or lower latency. |
| Kill criterion | Precision does not move. Then keep only the deterministic checks, which still remove the timeouts. |
| Evidence | CoVe raised list precision from 0.17 to 0.36 by verifying independent questions [primary]. MiniCheck reached 74.7% balanced accuracy, close to GPT-4, at about 400 times lower cost [primary]. uReview and RovoDev both filter before any LLM judge [primary]. |
| Effort | Human baseline 5-7 days. AI-assisted 2-3 days. |
| Benchmark cost | Two A-only runs, estimated at about $30-35. |

#### P1.3 · Specificity diagnostic, then the matching fix

| Field | Detail |
|---|---|
| Hypothesis | Specificity is 0.35 either because agents never find exact lines or because synthesis drops the lines they found. |
| Change | Count file:line references in agent output against those in the final answer, from existing data. If synthesis drops them, add an anchor-or-drop rule, so every code claim carries an anchor from the bundle or is hedged. If agents never find them, add file-to-symbol-to-line localisation in the code agent. |
| Metric | Specificity, together with the E0.3 anchor resolution rate. |
| Target | Specificity up by at least 0.1 while at least 95% of anchors resolve. |
| Kill criterion | Specificity rises while resolution falls. That means the rule produced made-up anchors, so revert it. |
| Evidence | TrajEval found 60-69% of failed agent runs had reached the right functions [primary], so dropping found evidence is a real failure mode. Agentless localised hierarchically and solved 32.0% of SWE-bench Lite at $0.70 per task [primary]. |
| Effort | Diagnostic, human baseline 1 day and AI-assisted 0.5 day. Fix, human baseline 3-5 days and AI-assisted 1-3 days. |
| Benchmark cost | $0 for the diagnostic, then one A-only run estimated at about $15-17. |

#### P1.4 · Sufficient-context gate that names the code owner

| Field | Detail |
|---|---|
| Hypothesis | Answering "could not determine, ask this code owner" when evidence is thin raises accuracy on the answers given without lowering recall. |
| Change | Before synthesis, check whether the bundle supports the question. When it does not, return a fixed abstention naming the code owner from CODEOWNERS or Backstage, which `repo_ownership` already holds. |
| Metric | Abstention rate on the E0.4 set, and recall on the 12 questions. |
| Target | At least 80% abstention on the unanswerable set, q12 says "could not determine", and recall holds within the noise floor. |
| Kill criterion | More than 1 of the 12 answerable questions abstains. |
| Evidence | Google's sufficient-context gate raised selective accuracy by 2-10 points [primary]. Uber found document quality, not retrieval, was Genie's ceiling [primary]. Semantic entropy detected confabulation at AUROC 0.79 [primary]. |
| Effort | Human baseline 4 days. AI-assisted 2 days. |
| Benchmark cost | Estimated at about $15-20. |

#### P1.5 · Structured evidence handoff

| Field | Detail |
|---|---|
| Hypothesis | Agents that hand synthesis structured records of claim, path, span, quote and tier, instead of prose, lose fewer facts and make deterministic verification possible. |
| Change | Agents emit the records, synthesis consumes them, and the verifier checks each quote against the clone. |
| Metric | Recall, specificity, and the share of claims the verifier settles without an LLM. |
| Target | Recall and specificity up by more than the noise floor, and at least 70% of claims settled deterministically. |
| Kill criterion | There is no gain and prompts grow longer. |
| Evidence | SWE-QA traced 41.8% of failures to the handoff [primary]. Anthropic's research system has subagents write to shared artifacts so findings are not passed on by retelling [vendor]. |
| Effort | Human baseline 8-10 days. AI-assisted 4-5 days, because the coordinator is mature and large. |
| Benchmark cost | One full run, about $45-50 based on the late-2026 full-run cost. |

### 8.8 Phase 2, retrieval and structure

#### P2.1 · Retrieval matrix

| Field | Detail |
|---|---|
| Hypothesis | On the DeepCodeBench subset, hybrid lexical and dense retrieval beats vectors alone, and a local code-aware embedder or a local reranker adds more. |
| Change | Add Postgres full-text search with code-aware tokens, splitting camelCase, snake_case and paths, and `pg_trgm` over entities and the Confluence, Jira and Google Docs pages. Fuse it with pgvector by reciprocal rank fusion. Run five configs: lexical only, vectors only, hybrid, hybrid with a local reranker, and hybrid with a local code embedder plus contextual headers. The reranker and the embedder are small models that run on CPU, per decision D3, so code stays on the machine. |
| Metric | Judge score and recall at k of expected entities, per config. |
| Target | One config beats vectors alone by more than the noise floor. |
| Kill criterion | A leg that does not beat the noise floor is dropped. |
| Evidence | Anthropic's contextual retrieval cut retrieval failures from 5.7% to 1.9%, a vendor-reported number [vendor]. CodeRAG-Bench found BM25 about equal to dense retrieval, 93.2 against 93.0, at roughly 1,000 times lower cost [primary]. Removing BM25 from LocAgent dropped it from 88.3 to 75.2 [primary]. voyage-code-3 reports +13.8% over general embedders, a vendor-reported number [vendor]. It is an API model, which decision D3 passed over, so it shows a code-aware embedder can help, not that a local one will. |
| Effort | Human baseline 6-8 days. AI-assisted 3 days. |
| Benchmark cost | Five A-only runs on the external set, estimated at about $60-85. |

#### P2.2 · Typed cross-service edges

| Field | Detail |
|---|---|
| Hypothesis | Edges taken from contracts and call sites raise blast-radius recall over name matching. |
| Change | Add deterministic extractors for OpenAPI, protobuf, GraphQL and AsyncAPI contracts, HTTP client calls, queue topics, database tables and env keys. Each edge records its source. |
| Metric | Blast-radius recall against the E0.6 truth, and edges per entity. |
| Target | Recall up by more than the noise floor. |
| Kill criterion | Recall does not move. |
| Evidence | Bito draws typed cross-repo edges [vendor] and Glean records provenance per edge [vendor]. The survey found no published accuracy for typed edges, so this rests on product practice. |
| Effort | Human baseline 8-10 days. AI-assisted 4 days. |
| Benchmark cost | $0 for extraction and the blast benchmark, plus one A-only run estimated at about $15-17. |

#### P2.3 · Tree-sitter symbol layer and repo map

| Field | Detail |
|---|---|
| Hypothesis | A parser-built symbol, import and call layer densifies the graph, and a PageRank repo map gives the router a better prior than node degree. |
| Change | Parse with tree-sitter per language and build an Aider-style PageRank map. Use SCIP where a build exists. LLM extraction stays as input for purpose and flows. Use personalised PageRank from the question's seeds for blast radius. |
| Metric | Edges per entity, router recall of expected repos, and judge score on the external set. |
| Target | A gain over the P2.1 winner. |
| Kill criterion | No gain over the P2.1 winner. |
| Evidence | LocAgent's graph reached Acc@5 94.2 against 61.7 for BM25, but only 35.2% outside its own benchmark [primary]. RepoGraph improved every host system it was added to [primary]. HippoRAG's PageRank gained about 20% on multi-hop questions [primary]. Aider's repo map has no isolated benchmark [primary]. |
| Effort | Human baseline 15-20 days. AI-assisted 6-8 days. |
| Benchmark cost | Two A-only runs, estimated at about $30-35. |

#### P2.4 · Context chosen by question intent

| Field | Detail |
|---|---|
| Hypothesis | Single-fact questions do worse with graph and community summaries in context, and multi-hop questions do better. |
| Change | Use the intent the orchestrator already assigns. Single-edge and ownership questions get chunks only. Impact and architecture questions get the graph and PageRank. |
| Metric | Judge score per intent. |
| Target | A gain on at least one intent with no loss on the others. |
| Kill criterion | No difference between intents. |
| Evidence | GraphRAG-Bench found chunks win simple facts, 60.9 against 60.1, and graphs win complex reasoning, 53.4 against 42.9, with prompts 10-100 times longer [primary]. RAPTOR found summary noise hurts fact questions [primary]. |
| Effort | Human baseline 5 days. AI-assisted 2-3 days. |
| Benchmark cost | Estimated at about $30-35. |

#### P2.5 · Compass files as retrieval targets

| Field | Detail |
|---|---|
| Hypothesis | Short per-module files help when agents retrieve them, and not when they are prepended to every prompt. |
| Change | Generate 25-35 line module files from handover docs and extraction output, index them, and never prepend them. |
| Metric | Tool calls per turn and judge score. |
| Target | Fewer tool calls at an equal or better score. |
| Kill criterion | No gain, or the files go stale faster than extraction refreshes them. |
| Evidence | Meta's compass files, described in [section 7](#7-where-the-evidence-disagrees) [primary]. RepoRepair generated docs at $0.12 per task and solved 282 of 300 [primary]. Meta: "Context that decays is worse than no context" [primary]. |
| Effort | Human baseline 5 days. AI-assisted 2 days. |
| Benchmark cost | Estimated at about $5 to generate and $30 to run. |

### 8.9 Phase 3, design bets

#### P3.1 · One hybrid index and one search tool

| Field | Detail |
|---|---|
| Hypothesis | One search tool over a pre-built index of every source beats per-source agents calling live MCP servers, on citation rate and latency. |
| Change | Start after the P2.1 matrix, per decision D4, and reuse the hybrid index P2.1 builds. Index Slack and GitHub PR text into it the way Confluence, Jira and Google Docs are already indexed offline, expose one search tool, and keep the per-source agents as fallbacks. |
| Metric | Citation rate of non-code sources, p50 latency and judge score. |
| Target | Non-code citation rate well above today's 0-5%, at lower latency and an equal score. |
| Kill criterion | The citation rate does not move. |
| Evidence | Dropbox replaced 5-10 tools with one [vendor], Vercel cut 17 tools to 2 [vendor], and SWE-QA's index beat its agentic system at under half the cost per correct answer [primary]. It needs benchmark questions that use non-code sources first. |
| Effort | Human baseline 20 days or more. AI-assisted 8-10 days. |
| Benchmark cost | Estimated at about $50-100. |

#### P3.2 · Source authority and conflict policy

| Field | Detail |
|---|---|
| Hypothesis | An explicit rule for code, docs and chat disagreeing, plus showing the conflict, beats silent weighting. |
| Change | bifrost's evidence tiers already rank code over config over docs. Add recency and author authority, seed stale docs in AcmeCo, and show unresolved conflicts in the answer. |
| Metric | Correct resolution on the seeded conflicts. |
| Target | At least 80% resolved correctly, with every unresolved conflict shown. |
| Kill criterion | No gain over the current tiers. |
| Evidence | Unblocked's conflict order, described in [section 5.3](#53-retrieval-structure-verification-and-evaluation) [vendor]. The CONFLICTS and WikiContradict benchmarks test conflict handling directly [primary]. |
| Effort | Human baseline 6 days. AI-assisted 3 days. |
| Benchmark cost | Estimated at about $15-20. |

#### P3.3 · Commit and PR "why" lineage

| Field | Detail |
|---|---|
| Hypothesis | Summaries of merged PRs and commits, indexed per file, answer historical questions better than a live GitHub agent. |
| Change | Summarise merged PRs and commits per file at index time and retrieve them by path. |
| Metric | Recall on the 2 historical questions and on the external set's history questions. |
| Target | Recall up by more than the noise floor. |
| Kill criterion | No gain on either set. |
| Evidence | Augment's Context Lineage summarises commits [vendor]. LinkAnchor found only about 42.2% of issues linked to their commits, at about 115k tokens per recovered link [primary]. |
| Effort | Human baseline 8 days. AI-assisted 3-4 days. |
| Benchmark cost | Estimated at about $15-20. |

### 8.10 Phase 4, new sources, after extending the AcmeCo seeds and questions

#### P4.1 · CI and deploys joined by commit SHA

| Field | Detail |
|---|---|
| Hypothesis | Joining deploys to code by SHA answers "what is running where" questions bifrost cannot answer today. |
| Change | Read deploy events through the Backstage annotations bifrost already parses and OpenTelemetry `vcs.*` attributes, keyed by SHA. |
| Metric | Recall on new deploy questions. |
| Target | Recall on the new deploy questions up by more than the noise floor. |
| Kill criterion | The seeded questions show no gain over today's answers. |
| Evidence | Backstage's well-known annotations and the OpenTelemetry `vcs` conventions [primary]. Unblocked indexes CI [vendor]. |
| Effort | Human baseline 10-15 days. AI-assisted 5-7 days. |
| Benchmark cost | Estimated at about $30-50. |

#### P4.2 · Incidents with similar-incident retrieval weighted by recency

| Field | Detail |
|---|---|
| Hypothesis | Recent similar incidents explain new failures better than code alone. |
| Change | Ingest incident records and retrieve similar ones with a recency weight. |
| Metric | Top-5 root-cause hit rate on seeded incidents. |
| Target | A top-5 hit rate above code-only answers by more than the noise floor. |
| Kill criterion | No gain over code-only answers. |
| Evidence | RCACopilot reached micro-F1 0.766 and found 93.8% of recurrences within 20 days [primary]. Meta's ranker found the root cause in the top 5 for 42% of investigations [primary]. A ReAct study found historical comments did not help [primary]. |
| Effort | Human baseline 10-15 days. AI-assisted 5-7 days. |
| Benchmark cost | Estimated at about $30-50. |

#### P4.3 · Feature-flag code references

| Field | Detail |
|---|---|
| Hypothesis | Linking flag keys to the code that reads them answers "what does this flag change" questions. |
| Change | Mine flag keys from code and join them to the flag service's catalog. |
| Metric | Recall on new flag questions. |
| Target | Recall above a plain grep by the code agent, by more than the noise floor. |
| Kill criterion | No gain over a plain grep by the code agent. |
| Evidence | LaunchDarkly ships an MCP server [vendor]. The survey found no accuracy evidence. |
| Effort | Human baseline 10-15 days. AI-assisted 5-7 days. |
| Benchmark cost | Estimated at about $30-50. |

Database migrations, infrastructure-as-code graphs and meeting transcripts stay off the list. The survey found no evidence that any of them improves engineering Q&A.

### 8.11 Phase 5, learning and trust

#### P5.1 · Feedback into eval scenarios before the graph

| Field | Detail |
|---|---|
| Hypothesis | Turning feedback into benchmark scenarios first, and gating the learning loop on non-regression, prevents the loop from learning noise. |
| Change | Convert corrections into eval questions, add implicit signals such as follow-up rephrasing and copied answers, and block a learning-loop update that regresses the benchmark. |
| Metric | Benchmark score before and after each learning batch. |
| Target | Learning batches raise the score by more than the noise floor, and every batch that lowers it is blocked. |
| Kill criterion | Updates do not move the score. |
| Evidence | Datadog cut label validation time by 95% and found noise injection lowered pass rate by 11%, vendor-reported numbers [vendor]. Users rated only 0.6% of turns in Cider [primary] and 3-4% in BlenderBot 3 [primary]. The survey found no controlled evidence that thumbs improve open Q&A. |
| Effort | Human baseline 6 days. AI-assisted 3 days. |
| Benchmark cost | Estimated at about $30-50. |

#### P5.2 · Bitemporal facts with raw pages kept

| Field | Detail |
|---|---|
| Hypothesis | Storing when a fact was true and when it was learned, and keeping the raw page beside the distilled one, fixes stale and lossy answers. |
| Change | Add valid-time and record-time to facts, and keep raw Confluence pages next to the Sonnet distillation. |
| Metric | Accuracy on seeded "what changed" questions. |
| Target | Accuracy on those questions up by more than the noise floor. |
| Kill criterion | No gain. |
| Evidence | Graphiti reported +18.5% on a memory benchmark, a vendor-reported number [vendor]. Verbatim retrieval beat extracted facts 67.4% to 45.4% [primary]. Memory systems dropped 37-64% on LongMemEval [primary]. |
| Effort | Human baseline 10 days. AI-assisted 4-5 days. |
| Benchmark cost | Estimated at about $30-50. |

#### P5.3 · Expert graph checked against CODEOWNERS

| Field | Detail |
|---|---|
| Hypothesis | Activity-based expertise names better code owners than CODEOWNERS for questions bifrost cannot answer. |
| Change | Validate first. Compare a time-decayed activity graph against CODEOWNERS on seeded questions before routing anything with it. |
| Metric | Agreement with the seeded right code owner. |
| Target | Agreement above CODEOWNERS by more than the noise floor. |
| Kill criterion | It does not beat CODEOWNERS. |
| Evidence | Nalanda built an expertise graph at Microsoft [primary]. LinkedIn's SIGIR 2024 system raised MRR by 77.6% [primary]. Unblocked time-decays its expert graph [vendor]. |
| Effort | Human baseline 5 days. AI-assisted 2 days. |
| Benchmark cost | Estimated at about $15-30. |

### 8.12 Standing security gate before multi-user use

**Mirror source permissions, and treat ingested Slack, Jira and PR text as untrusted, before bifrost serves more than one user.**

- Research agents read that text while holding tools, and the task executor in `src/bifrost/actions/executor.py` runs with `bypassPermissions` [read].
- Slack AI was shown to leak private-channel data through instructions planted in public messages [secondary].
- Glean enforces permissions per edge [vendor] and Onyx mirrors source permissions [vendor].
- This gate is not ranked, because it gates deployment rather than answer quality.

### 8.13 Effort and benchmark cost by phase

| Phase | Items | Human baseline days | AI-assisted days | Benchmark cost, estimated |
|---|---|---|---|---|
| 0 | E0.1-E0.4 | 13-16 | 6.5 | judge $10-25, runs about $30-35 |
| 0b | E0.5, E0.6 | 10-12 | 5 | extraction $30-300, runs $15-35 |
| 1 | P1.1-P1.5 | 22-29 | 10-14 | about $120-140 |
| 2 | P2.1-P2.5 | 39-48 | 17-20 | about $170-210 |
| 3 | P3.1-P3.3 | 34 or more | 14-17 | about $80-140 |
| 4 | P4.1-P4.3 | 30-45 | 15-21 | about $90-150 |
| 5 | P5.1-P5.3 | about 21 | 9-10 | about $75-130 |

The gap between the two effort columns is widest for greenfield pieces such as the judge, the extractors and the retrieval matrix. It is narrowest for the coordinator refactor and for labelling. Review, labelling and benchmark wall-clock do not shrink with an agent.

- A full A/B/C run on the 12-question AcmeCo set cost about $44-51 in model usage in late 2026. Run A is cold cache, run B is warm cache with a fresh conversation, and run C is warm cache with paraphrased questions, as defined in `src/bifrost/benchmarks/runner.py`.
- An A-only run is estimated at about a third of a full run, so $15-17. That figure is inferred and listed under [Not yet proven](#10-not-yet-proven).
- Phase 0's judge work re-scores existing runs, so it needs no new benchmark runs.

---

## 9. Not doing now

- Fine-tuned rankers or RL-trained retrieval, such as Cognition's SWE-grep or the CoRNStack and SweRank rerankers. They need labelled data bifrost does not have.
- Late-interaction, ColBERT-style retrieval for code, such as LateOn-Code. It needs a vector store with MaxSim scoring, and pgvector is believed to have none. That belief is listed under [Not yet proven](#10-not-yet-proven).
- A graph database at query time. Dropbox dropped one, and bifrost's Postgres graph already serves bounded walks.
- LLM-generated Cypher or graph queries. CodexGraph spent about 7 times the tokens of BM25 [primary].

---

## 10. Not yet proven

| Claim | Why it is unproven | What settles it |
|---|---|---|
| Exported runs carry the full answer text, citations and bundles that E0.2 and E0.3 need | The exports were not opened | Open one exported run and check its fields |
| An A-only run costs about a third of a full run | Inferred from three run kinds per run | Group `llm_cost_log` by run kind |
| Zero dispatch on q3, q4, q5 and q12 comes from the planner | The fast path could also cause it | Read `agent_results` for those turns, which is P1.1 step one |
| AcmeCo questions barely exercise non-code sources | The expected entities were not grepped | Count Jira and Confluence URNs in `acme.yml` |
| The learning loop writes confidence deltas during benchmark runs | If it does, warm runs B and C are contaminated | Check `confidence_deltas` rows inside a run window |
| AcmeCo is too small to show retrieval gains | Rests on one long-context study | Run P2.1 on AcmeCo and the external set |
| Cursor removed its embedding index | Docs say no embeddings are stored, and the removal story is unverified | A dated Cursor changelog entry |
| Continue was acquired | One unverified secondary report | An announcement from either company |
| pgvector has no MaxSim scoring | Stated from memory, not checked | pgvector docs and extensions |
| The small local checker, reranker and code embedder run on CPU fast enough for benchmark turns | Decision D3 assumed it, and none of them has been timed | Time one benchmark turn with each model loaded |
| Judge cost of $10-25 and extraction cost of $30-300 for the DeepCodeBench repos | Estimated from the handbook and call counts | A 5-question pilot |
| Typed edges and flag references are worth their cost | No published accuracy | P2.2 and P4.3 |
| Migrations, IaC and meeting transcripts help | No evidence found either way | A seeded question set, if ever needed |
| Expert routing beats CODEOWNERS | Product claims only | P5.3 |
| Some current anchors are made up | The scorer never checks them | E0.3 |
| A-only results predict full-run results | Warm runs could behave differently | Compare A against A/B/C deltas in the existing runs |

---

## 11. Risks

- [`docs/benchmarks/acme.yml`](benchmarks/acme.yml) is frozen, so every new question set goes in a separate file, and the benchmark isolation test in `tests/test_prompt_benchmark_isolation.py` has to keep passing.
- Prompt edits are guarded by the golden test, the few-shot shape tests and the benchmark isolation test. A Phase 1 change that touches prompts updates those tests in the same commit and never weakens them.
- Judge bias can make a gain look real. Swapped order, the median of 3 and the human labels in E0.2 are the guard.
- Untrusted ingested text can reach agents that hold tools. The standing gate covers it before any multi-user use.
- Tuning against 12 questions overfits. Phase 0b exists so Phase 2 decisions are made on questions bifrost has never seen.
- Small local models add model downloads and install weight, which decision D3 accepted. P2.1 drops the local reranker or embedder if it does not beat the noise floor, and P1.2 falls back to deterministic checks if precision does not move.

---

## 12. Open prerequisites

- About 1 day of human labelling, covering about 50 answer-fact pairs for E0.2. A judge cannot be calibrated without it, and under decision D1 Phase 1 waits for the judge.
- Model-usage spend during Phase 0, not after it. P1.1's run, estimated at about $15-17, and the E0.2 judge calls, estimated at $10-25, come to about $25-42 together. Phase 1 then needs an estimated $120-140.

---

## 13. Decisions taken (2026-11-05)

All four decisions were taken on 2026-11-05. The plan in [section 2](#2-decided-plan) follows from them.

### D1 · Does Phase 1 wait for the Phase 0 judge?

- **Chosen.** Measure first. Phase 1 waits for E0.1-E0.3, and the planner floor ships now as a bug fix.
- **Reason.** Every later gain is read against a noise floor and a calibrated judge.
- **Passed over.** Running Phase 1 in parallel would start P1.2-P1.5 while the judge is built. It is faster, but early results would be read through the regex scorer.
- **Passed over.** Keeping the current scorer would skip E0.2 and use the regex scorer throughout. It is cheapest, but gains under about 0.01 would stay unreadable.

### D2 · Which external question set does Phase 0b use?

- **Chosen.** The DeepCodeBench subset. E0.5 takes 30-50 questions from its 1-2 smallest repos, which caps the extraction spend.
- **Reason.** Its PR-derived questions come with fact lists, which match the E0.2 judge. A vendor built it, so taking its 1-2 smallest repos caps extraction cost.
- **Passed over.** A SWE-QA subset is an academic set of 576 questions. Its repos are larger, so extraction costs more.
- **Passed over.** Extending AcmeCo only has no extraction spend. It cannot show retrieval gains or memorisation effects.

### D3 · May bifrost take local model dependencies beyond `claude -p` and MiniLM?

- **Chosen.** Yes, small local models. E0.3 and P1.2 use a small local claim checker, and P2.1 tests a local reranker and a local code embedder, all running on CPU.
- **Reason.** Code stays on the machine. The cost is extra model downloads and install weight.
- **Passed over.** API only would use voyage-code-3 for embeddings and Claude for checks. It is simpler to run, but code leaves the machine and every run costs more.
- **Passed over.** No new dependencies would stay on `claude -p` and MiniLM. P2.1 would shrink to lexical against vectors, and P1.2 would lose its cheap checker.

### D4 · When does the unified-index bet (P3.1) start?

- **Chosen.** After the retrieval matrix. P3.1 starts once P2.1 is done and reuses its hybrid index, so Phase 3 is on the plan.
- **Reason.** P3.1 reuses the hybrid index P2.1 builds, and Confluence, Jira and Google Docs are already indexed offline. It needs non-code questions seeded first.
- **Passed over.** Starting now as a spike would give an early read on the non-code citation rate. It competes with Phases 0 and 1 for the same benchmark spend, and builds an index P2.1 would rebuild.
- **Passed over.** Not planning it keeps one agent per source. Non-code sources would stay at about 0-5% citation.

---

## 14. Sources

### Commercial products

- [Sourcegraph, how Cody understands your codebase](https://sourcegraph.com/blog/how-cody-understands-your-codebase) [vendor]
- [Glean, how code search works](https://docs.glean.com/security/how-code-search-works) [vendor]
- [Unblocked, inside the context engine](https://getunblocked.com/blog/inside-the-unblocked-context-engine/) [vendor]
- [Greptile, graph-based codebase context](https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context) [vendor]
- [Augment, real-time index](https://www.augmentcode.com/blog/a-real-time-index-for-your-codebase-secure-personal-scalable) [vendor]
- [Augment, Context Lineage](https://www.augmentcode.com/blog/announcing-context-lineage) [vendor]
- [Devin DeepWiki docs](https://docs.devin.ai/work-with-devin/deepwiki) [vendor]
- [Cognition SWE-grep](https://cognition.com/blog/swe-grep) [vendor]
- [Cursor, secure codebase indexing](https://cursor.com/blog/secure-codebase-indexing) [vendor]
- [Cursor, search tool docs](https://cursor.com/docs/agent/tools/search) [vendor]
- [Cursor, semantic search](https://cursor.com/blog/semsearch) [vendor]
- [Embeddings against grep, the unverified removal report](https://dbggr.dev/articles/embeddings-vs-grep-agentic-search/) [secondary]
- [GitHub Copilot agentic memory](https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/) [vendor]
- [Atlassian Rovo code context](https://support.atlassian.com/rovo/docs/what-is-code-context/) [vendor]
- [Swimm static analysis](https://swimm.io/blog/how-swimm-uses-static-analysis-to-generate-quality-code-documentation) [vendor]
- [CodeRabbit review context engine](https://www.coderabbit.ai/blog/explainable-reviews-coderabbit-review-context-engine) [vendor]
- [Qodo DeepCodeBench](https://www.qodo.ai/blog/deepcodebench-real-world-codebase-understanding-by-qa-benchmarking/) [vendor]
- [Tabnine context platform](https://context.tabnine.com/blog/the-platform-between-intent-and-execution/) [vendor]
- [Moderne Prethink](https://moderne.ai/blog/changing-ai-code-context-moderne-prethink) [vendor]
- [Bloop, archived January 2025](https://github.com/BloopAI/bloop) [primary]
- [Potpie](https://github.com/potpie-ai/potpie) [primary]
- [Google Code Wiki](https://developers.googleblog.com/introducing-code-wiki-accelerating-your-code-understanding/) [vendor]
- [Bito AI Architect](https://bito.ai/blog/bito-ai-architect/) [vendor]
- [Driver, microservices](https://www.driver.ai/use-cases/microservices) [vendor]
- [Dosu, August 2025](https://dosu.dev/blog/august-2025-dosu-drop) [vendor]
- [Factory Code Droid technical report](https://factory.ai/news/code-droid-technical-report) [vendor]

### In-house builds

- [Uber Genie](https://www.uber.com/blog/genie-ubers-gen-ai-on-call-copilot/) [primary]
- [Uber EAg-RAG](https://www.uber.com/blog/enhanced-agentic-rag/) [primary], also published as a [regional copy](https://www.uber.com/us/en/blog/enhanced-agentic-rag/)
- [Uber uReview](https://www.uber.com/blog/ureview/) [primary]
- [Uber, efficient software factory](https://www.uber.com/us/en/blog/efficient-software-factory/) [primary]
- [Spotify AiKA](https://thenewstack.io/introducing-aika-backstage-portal-ai-knowledge-assistant/) [secondary]
- [Spotify AiKA data plugins](https://backstage.spotify.com/discover/blog/aika-data-plugins-coming-to-portal) [vendor]
- [Spotify Honk, context engineering part 2](https://engineering.atspotify.com/2025/11/context-engineering-background-coding-agents-part-2) [primary]
- [Dropbox Dash, knowledge graphs, MCP and DSPy](https://dropbox.tech/machine-learning/vp-josh-clemm-knowledge-graphs-mcp-and-dspy-dash) [vendor]
- [Dropbox Dash, context engineering](https://dropbox.tech/machine-learning/how-dash-uses-context-engineering-for-smarter-ai) [vendor]
- [Meta, AI for incident response](https://engineering.fb.com/2024/06/24/data-infrastructure/leveraging-ai-for-efficient-incident-response/) [primary]
- [Meta DrP](https://engineering.fb.com/2025/12/19/data-infrastructure/drp-metas-root-cause-analysis-platform-at-scale/) [primary]
- [Meta, mapping tribal knowledge](https://engineering.fb.com/2026/04/06/developer-tools/how-meta-used-ai-to-map-tribal-knowledge-in-large-scale-data-pipelines/) [primary]
- [Microsoft RCACopilot](https://arxiv.org/abs/2305.15778) [primary]
- [Microsoft Triangle](https://www.microsoft.com/en-us/research/wp-content/uploads/2025/02/TRIANGLE_FSE25.pdf) [primary]
- [Microsoft Nissist](https://arxiv.org/abs/2402.17531) [primary]
- [Microsoft AI code reviews](https://devblogs.microsoft.com/engineering-at-microsoft/enhancing-code-quality-at-scale-with-ai-powered-code-reviews/) [primary]
- [Google Auto-Diagnose](https://arxiv.org/abs/2604.12108) [primary]
- [Google incident summaries](https://security.googleblog.com/feeds/posts/default/3832394459554071895) [primary]
- [Google, AI in software engineering](https://research.google/blog/ai-in-software-engineering-at-google-progress-and-the-path-ahead/) [primary]
- [Atlassian RovoDev code reviewer](https://arxiv.org/abs/2601.01129) [primary]
- [Atlassian HULA](https://arxiv.org/abs/2411.12924) [primary]
- [Grab, agents at scale](https://engineering.grab.com/how-grab-builds-and-runs-ai-agents-at-scale) [primary]
- [Grab, the birth of GrabGPT](https://engineering.grab.com/the-birth-of-grab-gpt) [primary]
- [Zalando, postmortem analysis](https://engineering.zalando.com/posts/2025/09/dead-ends-or-data-goldmines-ai-powered-postmortem-analysis.html) [primary]
- [Wix AirBot](https://www.wix.engineering/post/when-ai-becomes-your-on-call-teammate-inside-wix-s-airbot-that-saves-675-engineering-hours-a-month) [primary]
- [Netflix Pensive, returned 403](https://netflixtechblog.com/making-failure-diagnosis-reliable-with-pensive-ai-6b0811a1c015) [primary]
- [Stripe Minions](https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents) [primary]
- [Vercel, removing 80% of agent tools](https://vercel.com/blog/we-removed-80-percent-of-our-agents-tools) [vendor]
- [Ramp background agent](https://engineering.ramp.com/post/why-we-built-our-background-agent) [primary]

### Open-source tools and papers

- [Aider repo map](https://aider.chat/docs/repomap.html) [primary]
- [Aider repo map, generated walkthrough](https://deepwiki.com/Aider-AI/aider/4.1-repository-mapping-system) [secondary]
- [SCIP and stack-graphs status](https://github.com/orgs/sheeptechnologies/discussions/4) [secondary]
- [LSIF](https://lsif.dev/) [primary]
- [RepoGraph](https://arxiv.org/abs/2410.14684) [primary]
- [CodexGraph](https://arxiv.org/abs/2408.03910) [primary]
- [LocAgent](https://arxiv.org/abs/2503.09089) [primary]
- [LocAgent, outside-benchmark check](https://arxiv.org/abs/2509.25242) [primary]
- [CodeCompass](https://arxiv.org/abs/2602.20048) [primary], with [code](https://github.com/tpaip607/research-codecompass)
- [LARGER](https://arxiv.org/abs/2605.16352) [primary]
- [Architecture descriptors](https://arxiv.org/abs/2604.13108) [primary]
- [Agentless](https://arxiv.org/abs/2407.01489) [primary]
- [AutoCodeRover](https://arxiv.org/abs/2404.05427) [primary], with [code](https://github.com/AutoCodeRoverSG/auto-code-rover)
- [Moatless tools](https://github.com/aorwall/moatless-tools) [primary]
- [SWE-Search](https://arxiv.org/abs/2410.20285) [primary]
- [RepoCoder](https://arxiv.org/abs/2303.12570) [primary]
- [CodeRAG-Bench](https://arxiv.org/abs/2406.14497) [primary]
- [GraphCodeAgent](https://arxiv.org/abs/2504.10046) [primary]
- [voyage-code-3](https://blog.voyageai.com/2024/12/04/voyage-code-3/) [vendor]
- [nomic-embed-code](https://www.nomic.ai/news/introducing-state-of-the-art-nomic-embed-code) [vendor]
- [jina code embeddings](https://arxiv.org/abs/2508.21290) [primary]
- [CodeSage](https://arxiv.org/abs/2402.01935) [primary]
- [CoIR](https://arxiv.org/abs/2407.02883) [primary]
- [CORE-Bench](https://arxiv.org/abs/2606.11864) [primary]
- [LateOn-Code](https://huggingface.co/blog/lightonai/colgrep-lateon-code) [vendor]
- [CoRNStack](https://arxiv.org/abs/2412.01007) [primary]
- [SweRank](https://arxiv.org/abs/2505.07849) [primary]
- [Anthropic contextual retrieval](https://www.anthropic.com/news/contextual-retrieval) [vendor]
- [GraphRAG](https://arxiv.org/abs/2404.16130) [primary]
- [LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/) [vendor]
- [GraphRAG-Bench](https://arxiv.org/abs/2506.05690) [primary]
- [RAG against GraphRAG](https://arxiv.org/abs/2502.11371) [primary]
- [LightRAG](https://arxiv.org/abs/2410.05779) [primary]
- [LightRAG, unbiased re-judging](https://arxiv.org/abs/2506.06331) [primary]
- [HippoRAG](https://arxiv.org/abs/2405.14831) [primary]
- [HippoRAG 2](https://arxiv.org/abs/2502.14802) [primary]
- [RAPTOR](https://arxiv.org/abs/2401.18059) [primary]
- [SWE-agent](https://arxiv.org/abs/2405.15793) [primary]
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) [primary]
- [OpenHands](https://arxiv.org/abs/2407.16741) [primary]
- [SWE-bench-Live](https://arxiv.org/abs/2505.23419) [primary]
- [Claude Code grep-first, post by its lead](https://x.com/bcherny/status/2017824286489383315) [primary]
- [Claude Code grep-first, write-up](https://vadim.blog/claude-code-no-indexing/) [secondary]
- [Keyword-only agents](https://arxiv.org/abs/2602.23368) [primary]
- [Grep on LongMemEval](https://arxiv.org/abs/2605.15184) [primary]
- [SWE-QA agentic against index](https://arxiv.org/abs/2608.01507) [primary]
- [Anthropic multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) [vendor]
- [Adaptive-RAG](https://arxiv.org/abs/2403.14403) [primary]
- [Continue `@Codebase` deprecation](https://docs.continue.dev/reference/deprecated-codebase) [primary]
- [Continue acquisition report, unverified](https://aiagentduo.com/agents/continue-dev/) [secondary]
- [Long context against RAG](https://openreview.net/forum?id=pmcWo9DtDw) [primary]
- [CoReQA](https://arxiv.org/abs/2501.03447) [primary]
- [NoLiMa](https://arxiv.org/pdf/2510.04905) [primary]
- [RepoAgent](https://arxiv.org/abs/2402.16667) [primary]
- [deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open) [primary]
- [CodeWiki](https://arxiv.org/abs/2510.24428) [primary], with [code](https://github.com/FSoft-AI4Code/CodeWiki)
- [ETH AGENTS.md study](https://arxiv.org/abs/2602.11988) [primary]
- [RepoRepair](https://arxiv.org/abs/2603.01048) [primary]
- [MiniCheck](https://arxiv.org/abs/2404.10774) [primary]
- [MiniCheck model](https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B) [vendor]
- [Chain-of-Verification](https://arxiv.org/abs/2309.11495) [primary]
- [ALCE](https://arxiv.org/abs/2305.14627) [primary]
- [TrajEval](https://arxiv.org/abs/2603.24631) [primary]
- [SWE-bench Verified retirement](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) [vendor]
- [SWE-bench Pro](https://arxiv.org/abs/2509.16941) [primary]
- [SchrodingerRepo](https://arxiv.org/abs/2609.27891) [primary]
- [SWE-QA](https://arxiv.org/abs/2509.14635) [primary]
- [Code-QA-Bench](https://arxiv.org/abs/2605.29277) [primary]
- [SWE Atlas](https://arxiv.org/abs/2605.08366) [primary]
- [CodeQueries](https://arxiv.org/abs/2209.08372) [primary]
- [RepoQA](https://arxiv.org/abs/2406.06025) [primary]
- [Long Code Arena](https://arxiv.org/abs/2406.11612) [primary]
- [CrossCodeEval](https://arxiv.org/abs/2310.11248) [primary]
- [RepoBench](https://arxiv.org/abs/2306.03091) [primary]
- [RAGAS](https://arxiv.org/abs/2309.15217) [primary]
- [ARES](https://arxiv.org/abs/2311.09476) [primary]
- [AutoNuggetizer](https://arxiv.org/abs/2504.15068) [primary]
- [Bias-corrected reporting](https://arxiv.org/abs/2511.21140) [primary]

### Organisational knowledge sources and trust

- [Unblocked](https://getunblocked.com/) [vendor]
- [CommitEval](https://arxiv.org/abs/2511.19875) [primary]
- [Rationale spread across artifacts](https://arxiv.org/abs/2604.10345) [primary]
- [Lore](https://arxiv.org/abs/2603.15566) [primary]
- [ADRs for AI agents](https://www.andrewconnell.com/articles/architecture-decision-records-ai-agents/) [secondary]
- [ADR repository](https://github.com/architecture-decision-record/architecture-decision-record) [primary]
- [ReAct study on historical comments](https://arxiv.org/abs/2403.04123) [primary]
- [incident.io AI SRE](https://incident.io/blog/introducing-ai-sre) [vendor]
- [FireHydrant retrospectives](https://docs.firehydrant.com/docs/intro-to-retrospectives) [vendor]
- [Grafana knowledge graph for RCA](https://grafana.com/blog/knowledge-graph-as-context-for-llms-demonstrating-decisive-rca-and-faster-production-performance/) [vendor]
- [Datadog Bits AI SRE](https://www.datadoghq.com/blog/bits-ai-sre/) [vendor]
- [Datadog eval platform](https://www.datadoghq.com/blog/engineering/bits-ai-eval-platform/) [vendor]
- [Sentry Seer](https://blog.sentry.io/sentry-ai-debugger-autofix-superpower-traces/) [vendor]
- [Resolve](https://resolve.ai/blog/how-we-built-resolve-ai) [vendor]
- [LaunchDarkly MCP](https://launchdarkly.com/docs/home/getting-started/mcp) [vendor]
- [Apollo MCP server](https://www.apollographql.com/docs/apollo-mcp-server) [vendor]
- [CloudQuery](https://www.cloudquery.io/blog/steampipe-vs-cloudquery) [vendor]
- [Port AI cost research](https://www.port.io/blog/research-how-we-cut-ai-costs-by-80-percent) [vendor]
- [OpsLevel context layer](https://www.opslevel.com/product/context-layer) [vendor]
- [GUAC](https://openssf.org/blog/2024/03/07/guac-joins-openssf-as-incubating-project/) [primary]
- [Glean meeting transcripts](https://docs.glean.com/user-guide/knowledge/meetings-and-calendar/zoom-meeting-transcripts) [vendor]
- [Backstage well-known annotations](https://backstage.io/docs/features/software-catalog/well-known-annotations/) [primary]
- [OpenTelemetry `vcs` attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/vcs/) [primary]
- [LinkAnchor](https://arxiv.org/abs/2508.12232) [primary]
- [Issue-commit linking basics](https://arxiv.org/abs/2507.09199) [primary]
- [Nalanda](https://arxiv.org/abs/2110.08403) [primary]
- [CORAL](https://arxiv.org/abs/2202.02385) [primary]
- [Atlassian Teamwork Graph](https://developer.atlassian.com/platform/teamwork-graph/relationships/) [vendor]
- [LinkedIn SIGIR 2024](https://arxiv.org/abs/2404.17723) [primary]
- [Graphiti and Zep](https://arxiv.org/abs/2501.13956) [vendor]
- [Mem0](https://arxiv.org/abs/2504.19413) [vendor]
- [Verbatim against extracted memory](https://arxiv.org/abs/2601.00821) [primary]
- [LongMemEval](https://arxiv.org/abs/2410.10813) [primary]
- [Stack Overflow content health](https://stackoverflow.blog/2021/11/17/introducing-content-health-a-new-way-to-keep-the-knowledge-base-up-to-date/) [vendor]
- [Stack Overflow content health, press note](https://stackoverflow.co/company/press/archive/content-health/) [vendor]
- [Stack Internal](https://stackoverflow.co/internal/features/) [vendor]
- [ALiiCE](https://arxiv.org/abs/2406.13375) [primary]
- [Sufficient context](https://arxiv.org/abs/2411.06037) [primary]
- [Semantic entropy](https://www.nature.com/articles/s41586-024-07421-0) [primary]
- [OP-SCORTEX](https://arxiv.org/abs/2606.20758) [primary]
- [OP-SCORTEX, related](https://arxiv.org/abs/2606.08590) [primary]
- [DOCER](https://arxiv.org/abs/2212.01479) [primary]
- [Stale agent configs](https://arxiv.org/abs/2606.09090) [primary]
- [DocDrift](https://github.com/N-Garai/DocDrift) [primary]
- [CONFLICTS](https://arxiv.org/abs/2506.08500) [primary]
- [WikiContradict](https://arxiv.org/abs/2406.13805) [primary]
- [Onyx permissions](https://docs.onyx.app/admins/permissions/understanding_permissions) [vendor]
- [Glean DLP critique](https://www.deepinspect.ai/blog/glean-dlp) [secondary]
- [Slack AI data exfiltration](https://promptarmor.substack.com/p/data-exfiltration-from-slack-ai-via) [secondary]
- [RAG PII leakage](https://arxiv.org/abs/2402.16893) [primary]
- [Google Code Wiki coverage](https://www.theregister.com/2025/11/17/google_previews_code_wiki/) [secondary]
- [Cider feedback rate](https://arxiv.org/abs/2509.18361) [primary]
- [BlenderBot 3](https://arxiv.org/abs/2208.03188) [primary]
- [Greptile comment quality](https://www.zenml.io/llmops-database/improving-ai-code-review-bot-comment-quality-through-vector-embeddings) [secondary]
- [Greptile learning system](https://www.greptile.com/docs/code-review/training-the-learning-system) [vendor]
- [CodeRabbit learnings](https://docs.coderabbit.ai/knowledge-base/learnings) [vendor]
- [Google, resolving review comments with ML](https://research.google/blog/resolving-code-review-comments-with-ml/) [primary]
