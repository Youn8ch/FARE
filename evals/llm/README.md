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

The semantic business acceptance runner uses the real HTTP model configured in
`config/fare.yaml`, keeps both shadow stages enabled by default, repeats each
positive/negative case three times, writes an audit log, and exits non-zero when
an acceptance threshold is missed:

```powershell
.\.venv\Scripts\python.exe -m evals.llm.run_real_semantic_acceptance
```

Its checked-in cases are marked `candidate_pending_business_owner_approval`.
They provide an executable quality gate now, but must not be relabeled as gold
until the business/rule owner approves the wording and expected effects.

Interrupted runs can resume from the audit trail without repeating completed
case/repeat pairs:

```powershell
.\.venv\Scripts\python.exe -u -m evals.llm.run_real_semantic_acceptance `
  --resume-audit-directory audit_logs\real_semantic_<timestamp>
```
