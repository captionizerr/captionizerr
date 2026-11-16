# Contributing

Thanks for helping. Small, focused PRs are easiest to review.

- **Setup:** `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`, then `.venv/bin/pytest`. The media tests need `ffmpeg` on PATH.
- **Library safety is the one hard rule:** nothing may write into the media library except through `writer.py` (`write_next_to`, `replace_own`, `move_sidecars`). PRs that add other writes there won't be merged.
- **New providers** implement `complete(system, context, work) -> ProviderResult` and `count_tokens()`. Set `max_cues` if their output limit needs chunking, and `supports_batch` plus the batch methods if there's a batch API. See `providers/ollama.py` for a compact example.
- **Translation-quality changes** (prompt, validator): include before/after on a real episode (a few representative lines) and the cost difference. Never commit copyrighted subtitle files.
- **Tests** for behaviour changes. The fake providers in `tests/test_service.py` and `tests/test_m4.py` cover most flows without network calls.
