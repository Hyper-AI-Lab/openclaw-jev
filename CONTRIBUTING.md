# Contributing to Aura

Thanks for helping improve Aura.

## Ground rules

1. **No secrets** — never commit `settings.json`, API keys, Slack tokens, or `data/`.
2. **Keep changes surgical** — prefer small PRs that match existing architecture.
3. **Tests** — add or update pytest coverage for behavior changes.
4. **Docs** — update `ARCHITECTURE.md` or runbooks when control-plane behavior changes.

## Local checks

```bash
python3 -m venv venv && source venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
pytest -q
bash ops/verify_openclaw_patch.sh   # when OpenClaw dist patches change
```

## PR checklist

- [ ] Behavior change has a test or a clear reason it cannot
- [ ] `settings.example.json` updated if new config keys were added
- [ ] No live credentials or host-specific secrets in the diff
- [ ] README / ARCHITECTURE updated when the public contract changes
- [ ] Until the schema conventions land (WP-26 batch 6), no PR changes the database schema: no Alembic revision in `app/db/migrations/versions/`, and no change in `app/db/models.py` to a table, to a column's presence, type, nullability or server default, to an index or to a constraint (ordering rule 44). Python-side `default=` and `onupdate=` arguments are not schema and are not frozen.

## Code of collaboration

Be direct, kind, and specific. Prefer reproducible steps over screenshots alone.
