# FARE test suites

Default tests are deterministic and offline. The autouse network guard permits
only loopback sockets required by the Windows test runner and fails any external
connection attempt.

Version 2 evaluation cases live in `cases/evaluations/*.v2.json`. Every file uses
the envelope `{schema_version, suite, cases}`; each case contains explicit
`dependencies`, feature flags, request, LLM profile, and per-item structured
expectations. Case and request IDs are globally unique. The loader validates the
production splitter output and refuses ordinary end-to-end cases above four items.

LLM fixtures referenced by v2 cases live below `fixtures/llm/`, carry
`fixture_version` and `purpose`, and use exact symbolic item references such as
`@item:1`. Symbols are accepted only in `analyzed_item_ids`, `affected_item_ids`,
`scope`, and `item_id`; paths are resolved within the allowed fixture/policy roots.

`cases/evaluations/core.json` is retained as deprecated v1 data because external
consumer removal has not been approved. New scenarios must use v2.

Useful commands:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_evaluation_cases.py -q
.\.venv\Scripts\python.exe -m pytest -m llm_guard -q
.\.venv\Scripts\python.exe -m pytest -m llm_pipeline -q
.\.venv\Scripts\python.exe -m pytest -m llm_http -q
.\.venv\Scripts\python.exe -m pytest tests/test_llm_observability.py -q
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m ruff check . --no-cache
```

The real-model evaluation gate and provisional synthetic dataset are documented
in `evals/llm/README.md`; they are outside the default pytest collection path.
