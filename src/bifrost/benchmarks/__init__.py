"""Domain proving ground for the chat-research learning loop.

Tier 3 of the master plan. Modules:

- ``scorer``       — pure functions: recall, precision, specificity,
                     coverage, tier_integrity. No DB, no LLM. Tested.
- ``loader``       — read ``docs/benchmarks/<domain>.yml`` and upsert
                     into ``benchmark_questions``.
- ``runner``       — CLI: execute Run A (cold) / B (warm) / C (paraphrased)
                     across the loaded question set, score, persist.
"""
