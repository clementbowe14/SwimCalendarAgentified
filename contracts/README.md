# API Contracts

This directory contains generated OpenAPI and curated JSON fixtures for frontend contract tests.

Public fixtures represent token-scoped client invite flows. They must not include raw instructor IDs,
raw client IDs, private schedule details, internal appointment IDs, or provider error payloads.

Coach fixtures may include coach-only operational data and should only be consumed by authenticated
instructor experiences.

Regenerate contracts with:

```bash
./venv/bin/python scripts/export_contracts.py
```

Check for drift with:

```bash
./venv/bin/python scripts/export_contracts.py --check
```
