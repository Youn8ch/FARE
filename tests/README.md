# FARE test suites

Default tests are deterministic and offline. The autouse network guard permits
only loopback sockets required by the Windows test runner and fails any external
connection attempt.

Area-relation requirement batches live in `cases/network_requirements/`. The
dedicated suite automatically discovers all 30 versioned batches through the configured requirement
source and evaluates them with the versioned network-plan fixture, isolated test
policy bundle and mock LLM. It locks rule direction, exact area/Region/
platform/usage matching, specific-before-general rule order, per-item isolation,
lookup deduplication, and the single business-body 404 contract.

Four `area_relation_mixed_*.batch.json` files contain eight requirements each.
Every mixed file combines positive matches, reverse or near-miss traffic, and a
multi-source/multi-destination request; matching expected files keep the oracle
maintainable as more batches are added.

The base `area_relation_success.batch.json` contains 30 successful network-plan
requirements. Another 24 generated batches contain four mixed requirements each.
Across all batches the suite evaluates 159 requests and 261 items.

Version 2 evaluation cases live in `cases/evaluations/*.v2.json`. Every file uses
the envelope `{schema_version, suite, cases}`; each case contains explicit
`dependencies`, feature flags, request, LLM profile, and per-item structured
expectations. Case and request IDs are globally unique. The loader validates the
production splitter output and refuses ordinary end-to-end cases above four items.

LLM fixtures referenced by v2 cases live below `fixtures/llm/`, carry
`fixture_version` and `purpose`, and use exact symbolic item references such as
`@item:1`. Symbols are accepted only in `analyzed_item_ids`, `affected_item_ids`,
`scope`, and `item_id`; paths are resolved within the allowed fixture/policy roots.

`test_llm_business_effects.py` locks the model participation boundary with business
examples: approved duration/purpose conflicts can downgrade deterministic compliant
items, missing approval can only ask a question, normal traffic cannot be wrongly
downgraded, and the server effect policy overrides a model suggestion. It also
verifies that weak, unlinked, or unapproved findings remain auditable but cannot
affect the decision. `test_real_semantic_acceptance_scoring.py` verifies the repeated
real-model scorer and audit-resume behavior without network access.

`cases/evaluations/core.json` is retained as deprecated v1 data because external
consumer removal has not been approved. New scenarios must use v2.

Useful commands:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_evaluation_cases.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_network_requirement_area_relations.py -q
.\.venv\Scripts\python.exe -m pytest -m llm_guard -q
.\.venv\Scripts\python.exe -m pytest -m llm_pipeline -q
.\.venv\Scripts\python.exe -m pytest -m llm_http -q
.\.venv\Scripts\python.exe -m pytest tests/test_llm_observability.py -q
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m ruff check . --no-cache
```

The real-model evaluation gate and provisional synthetic dataset are documented
in `evals/llm/README.md`; they are outside the default pytest collection path.
