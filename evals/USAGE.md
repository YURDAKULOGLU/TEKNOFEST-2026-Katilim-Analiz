# Evaluation harness

The harness implements WP-070 gates `EVAL-001`, `003`, `004`, `005`, `006`, `007`, and `013`. It deliberately reports `insufficient_data` instead of passing when verified examples or class support are below the declared minimum.

From the repository root with the backend environment active:

```powershell
$env:PYTHONPATH = "backend/src"
python -m evals.security_eval
python -m evals run `
  --security-predictions evals/results/security-rules-only-v1.0.jsonl `
  --allow-incomplete
```

The command writes machine-readable JSON and a Markdown summary under `evals/results/`. Omit `--allow-incomplete` in CI; the command then exits non-zero unless every included blocking gate passes.

To repeat the bounded live collection:

```powershell
$env:PYTHONPATH = "backend/src"
python -m evals.live_collection
```

Live collection enforces the runtime collector's official-host allowlist, HTTPS, DNS/IP validation, fail-closed robots handling, redirect revalidation, per-host delay, bounded retry/backoff, response-size limit, and CAPTCHA/authentication refusal. Raw HTML is written only to ignored `data/private/wp070/raw`.

Versioned cached profiles can be supplied without giving a model network, SQL,
filesystem, or tool access. The security execution must be regenerated after
relevant product or harness changes, then passed explicitly to the main harness:

```powershell
python -m evals run `
  --predictions path/to/extraction-predictions.jsonl `
  --security-predictions evals/results/security-rules-only-v1.0.jsonl `
  --allow-incomplete
```

Prediction IDs must be unique. An extraction prediction contains `example_id`, flattened `fields`, canonical `candidate`, `evidence`, and the minimal `source_blocks` needed to verify its offsets. A security prediction additionally binds the observed decision and accepted evidence to its case, product-path, profile, source-tree, and execution digests.

The security command executes every reviewed case through the production
`ExtractionPipeline` with the model disabled and a fail-closed socket guard. It
records accepted evidence, authority signals, the case-suite digest, the
product-source digest, and the harness digest. The evaluator rejects edited or
stale cached executions; rerun the command after relevant product or harness
code changes.

## Current honest baseline

- Coverage: 10/10 explicit states (`4 success`, `6 blocked`).
- Turkish normalization: 16/16 cases pass.
- Golden comparison: 6/6 cases pass.
- Extraction, evidence/schema, and classification: `insufficient_data` because 0 bank examples are human-verified.
- Prompt-injection suite: 20/20 reviewed cases completed on the rules-only,
  no-network product path; `0` authority escapes and `0` unsupported fact
  acceptances, so `EVAL-013` passes for this declared profile.
- Fine-tuning: not authorized.
