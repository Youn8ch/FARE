# FARE LLM evaluation harness

This directory is outside the default `pytest` test path. The checked-in dataset is
fully synthetic and has `dataset_status: provisional`; it validates the evaluation
contract and scoring implementation only. It is not business gold data and its
results are not a model-quality claim.

Run the offline contract suite with:

```powershell
.\.venv\Scripts\python.exe -m pytest evals/llm/test_contract_dataset.py -q
```

Real-model evaluation requires all of the following: an explicitly selected
`evals/llm` test path, `RUN_REAL_LLM_EVAL=1`, an approved gold manifest, and
`LLM_BASE_URL` plus `LLM_MODEL` supplied by the secure environment. `LLM_API_KEY`
is optional according to the deployment. If any gate is absent, the real-model
test skips before creating a client or making a request.

