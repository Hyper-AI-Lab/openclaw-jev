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
pip install -r requirements.txt
pytest -q
bash ops/verify_openclaw_patch.sh   # when OpenClaw dist patches change
```

## PR checklist

- [ ] Behavior change has a test or a clear reason it cannot
- [ ] `settings.example.json` updated if new config keys were added
- [ ] No live credentials or host-specific secrets in the diff
- [ ] README / ARCHITECTURE updated when the public contract changes

## Code of collaboration

Be direct, kind, and specific. Prefer reproducible steps over screenshots alone.
