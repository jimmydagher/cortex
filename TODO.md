# TODO

- [ ] #1 Create the GitHub repo jimmydagher/cortex, push `main`, and confirm CI passes and publishes `ghcr.io/jimmydagher/cortex:0.1.0` — the workflow has never run on GitHub
- [ ] #2 Run the browser tests: `python -m playwright install chromium`, then `python -m pytest tests -m e2e` — written but not yet run
- [ ] #3 First run in a real browser: sign in, setup wizard, graph view, Synapse approve — the GUI hasn't been opened yet
- [ ] #5 OAuth so claude.ai custom connectors can connect — they can't send a bearer header
- [ ] #6 Optional git commit of the brain after `synapse_commit` — history of memory changes
- [ ] #7 A "check the brain" audit tool over MCP — the brain's audit runs only where its script exists
- [ ] #8 Edit notes in the GUI — today notes are read-only there

---

## Done
- [x] #4 Recreate the local `.venv` on Python 3.14 after the PATH fix and a VS Code restart (README › Run it locally) — the old uv-made environment was locked by VS Code — completed 2026-09-26 · VERSION 0.1.0
