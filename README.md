# Development Notes

- 2026-08-24: added a Claude Code PostToolUse hook (`.claude/settings.json`, script `.claude/hooks/sources_pytest_hook.py`) that runs pytest after Edit/Write/MultiEdit touches a `.py` file under a `sources/` directory, and prints the result.
- It runs `tests/test_<name>.py` or `tests/test_source_<name>.py` when one exists for the touched module, otherwise it falls back to the full suite (99 tests, <1s). Only `base.py` (via `test_source_base.py`) and `affiliate_feed.py` currently have a dedicated test file — `citilink.py` and `regard.py` do not, so edits to those fall back to the full suite.
