# Development conventions

- Python 3.12, uv, FastAPI server-rendered HTML. Do not introduce a frontend build toolchain.
- All fixtures and screenshots must use synthetic data. Never commit personal or customer data, secrets, database files or runtime outputs.
- Run tests with temporary isolated directories. Preserve account/project isolation and explicit human confirmation of quantities and prices.
- The public edition contains only the generic drawing-to-quotation workflow. Do not add private project profiles.
- Run `uv run pytest` and review staged files before publishing. New screenshots must come from the running application.
