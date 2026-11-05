@AGENTS.md

The canonical agent instruction lives in `AGENTS.md` (imported above).
Below is the minimum needed before reading it:

- Ferronit is a single package: Python `ferronit/` + one Rust core `ferronit-rs/` (→ `ferronit._core`),
  Python ≥ 3.12; the data layer is rawdb/rawmodel for PostgreSQL (asyncpg), MySQL (aiomysql) and
  SQLite (aiosqlite), all three drivers are optional extras.
- Rust changes require a rebuild: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 poetry run maturin develop --release`
  (from the repository root).
- Pre-commit check: `./scripts/check.sh` (ruff → mypy → core build → pytest).
- Types are mandatory, docstrings are English (Google-style), public API goes in `__all__`.
- Edit the version in two places: `ferronit/__init__.py` and `ferronit-rs/Cargo.toml`.
- Benchmarks: the primary competitor is BlackSheep; compare against it by default (details in AGENTS.md).
- Run benchmarks **only on performance (P) cores** unless the user explicitly asks otherwise:
  servers — `BENCH_SERVER_CPUS=0-5`, the `ab` client — `BENCH_CLIENT_CPUS=6-11`. Do not use the
  E-cores `12-19`. After a run, check the actual affinity (`taskset -pc <pid>`) and state the core
  layout in the chart caption / reply.
- Artifact delivery: send benchmark images and charts **only** to the Mattermost channel
  `ferronit-omp` (Ferronit-OMP, channel_id `kdsscsgqm7nu5mmte9m5ssop5r`). Never to `ferronit-pi`.
  How to send: `POST $MATTERMOST_URL/api/v4/files` (multipart: `channel_id`, `files`) →
  `POST $MATTERMOST_URL/api/v4/posts` (`channel_id`, `message`, `file_ids`), header
  `Authorization: Bearer $MATTERMOST_TOKEN`; the bot must be a member of the channel.
