# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Single-binary release per platform** (Issue [#14](https://github.com/Hochfrequenz/kuckuck/issues/14)).
  The four slim/NER/MCP/NER+MCP PyInstaller variants per OS have been collapsed into one fat `kuckuck_<os>` binary that includes CLI, MCP-server, and GLiNER PERSON detection.
  MCP clients now configure `command: "kuckuck", args: ["mcp", "serve"]` instead of a separate `kuckuck-mcp` command.
  Total release artifacts go from 8 to 2 (`kuckuck_windows_<ver>.exe`, `kuckuck_macos_arm64_<ver>`).

### Added

- **`kuckuck mcp serve` subcommand** delegating to the FastMCP server.
  The standalone `kuckuck-mcp` console script still works (pip-install backward compatibility); `kuckuck mcp serve` is the recommended invocation for new installs and the single-binary release.
- **Claude Code PreToolUse hook** (Issue [#9](https://github.com/Hochfrequenz/kuckuck/issues/9)).
  Shell (`integrations/claude-code/kuckuck-pseudo.sh`) and PowerShell (`integrations/claude-code/kuckuck-pseudo.ps1`) scripts that auto-pseudonymize `.eml` and `.msg` files before every `Read` / `Edit` / `Grep`.
  Default is fail-closed: missing `kuckuck`, missing `jq` (POSIX) or a failing run blocks the tool call.
  The escape hatch `KUCKUCK_HOOK_FAIL_OPEN=1` lets a run pass through for local triage (documented as UNSAFE).
  Stderr block-message points at `kuckuck_pseudonymize` via the MCP server so Claude learns the right remediation path instead of a bare "no".
  New CLI subcommand `kuckuck install-claude-hook` (with `--global` and `--uninstall`) copies the right script for the host OS into `.claude/hooks/` and merges the hook entry into `.claude/settings.json` idempotently without overwriting user hooks.
  Setup guide and example settings in `integrations/claude-code/`.
- **MCP server** `kuckuck-mcp` (Issue [#10](https://github.com/Hochfrequenz/kuckuck/issues/10)).
  Sub-package `src/kuckuck_mcp/`, optional extra `kuckuck[mcp]`, console-script `kuckuck-mcp`.
  Built on FastMCP `>= 3` (consistent with the Hochfrequenz MCP stack).
  **Five stdio tools**:
  - `kuckuck_pseudonymize` — file-path-based, defaults to `ner=auto` (best-effort PERSON detection when the `[ner]` extra and the model are available, regex-only fallback otherwise; no crash on minimal setups).
  - `kuckuck_restore` — gated behind FastMCP elicitation; user must confirm cleartext disclosure.
  - `kuckuck_fetch_model` — one-time downloader for the ~ 1.1 GB GLiNER snapshot, gated behind elicitation so a multi-GB transfer never starts silently.
  - `kuckuck_list_detectors` — metadata only.
  - `kuckuck_status` — aggregated `problems`-list with remediation hints (pattern from `Hochfrequenz/sap-mcp-config`).
  **Four discoverability prompts** (slash-menu quick-actions): `setup_kuckuck` (first-time-setup walkthrough), `pseudonymize_before_reading`, `diagnose_kuckuck_setup`, `explain_kuckuck_tokens`.
  All pseudonymization tools are `file_path`-based — a text-input variant would have leaked PII through the tool argument.
  No MCP resources exposed (mapping inspection stays local via `kuckuck inspect`).
  Tool-arguments are workspace-confined via `KUCKUCK_MCP_ALLOWED_ROOTS` (default `$PWD`), so the model cannot trigger writes outside the configured roots.
- Setup guides plus example configs for Claude Code, opencode and Claude Desktop in `integrations/mcp/`.
- AGENTS.md updated with the rule: read FastMCP docs before changing `src/kuckuck_mcp/`, return-types are pydantic `BaseModel` not `TypedDict`.

## [0.1.0] - 2026-04-22

First public release of Kuckuck.
The package provides a local-only pseudonymization pipeline for personally identifiable data in text files, with a CLI plus a small library API.
The mapping sidecar is AES-GCM encrypted with a master key kept outside the repo.

### Added

- **Regex detectors** for the most common PII shapes:
  e-mail addresses (regex + `email-validator` vetting), phone numbers (`phonenumbers`, default region DE), Jira/Confluence handles (`@user.name`, `[~accountid:…]`, `[~user]`), and a denylist for customer/project names with an Aho-Corasick fallback past 1000 entries.
- **Optional GLiNER PERSON detector** opted-in via `kuckuck run --ner` (separate `kuckuck[ner]` extra).
  Model snapshot is fetched once via `kuckuck fetch-model` into `~/.cache/kuckuck/models/`; non-default `--model-id` is gated behind `--allow-untrusted-model` because GLiNER weights are loaded via pickle.
- **Format-aware preprocessors** for `.eml`, `.msg`, Markdown, and XML/HTML, selectable via `kuckuck run --format` (default `auto` decides by file suffix).
  Mail headers, code fences, YAML frontmatter, XML structure, and CDATA wrappers are preserved through pseudonymisation.
- **Library API** `run_pseudonymize(paths, RunOptions(...))` so notebooks and scripts can drive the same pipeline as the CLI.
- **CLI subcommands** `kuckuck run`, `kuckuck restore`, `kuckuck init-key`, `kuckuck inspect`, `kuckuck list-detectors`, `kuckuck fetch-model`, `kuckuck version`, plus the implicit-`run` shortcut `kuckuck <file>`.
- **PyInstaller binaries** for Windows and macOS arm64 in two variants:
  slim (regex detectors only, ~26 MB) and NER (`kuckuck_*_ner_*`, bundles CPU-only torch + gliner, ~300 MB).
  Both ship from the GitHub release attachment on tag.
- **Stable exit codes** `0` ok / `1` generic / `2` usage / `3` key-not-found / `4` mapping-missing / `5` mapping-corrupt / `6` mapping-wrong-key / `7` model-missing.
- **Snapshot, hypothesis, and integration tests** including 25 GLiNER integration tests over common German first names, gated behind `pytest -m ner`.
- **CI matrix** Windows / macOS / Linux × Python 3.11, 3.12, 3.13, 3.14.
  Coverage gate at 80 %, currently 96 %.
  The GLiNER snapshot is cached via `actions/cache@v4` so the 1.1 GB download happens once per cache key.

### Security

- AES-GCM-encrypted mapping sidecar with the file path bound as AAD, HMAC-SHA256 deterministic tokens (8-hex truncation, collision counter up to 10 000).
- Strict 64-hex master-key validation; no passphrase fallback.
- XML preprocessor hardened against XXE and entity-bomb attacks (`resolve_entities=False`, `no_network=True`, `load_dtd=False`, explicit `huge_tree=False`).
- HTML body extractor strips `<script>` / `<style>` / `<noscript>` before reading text, so JS/CSS payloads do not flow through the detectors.
- `kuckuck fetch-model` validates the slug against `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and refuses paths that escape the cache root, blocking Windows backslash traversal.

### Known limitations

- `.msg` files are not round-tripped: the compound-document structure is dropped, output is a plain-text body.
- `.eml` headers may be re-folded by Python's `email` policy on rewrite (e.g. `Date: Mon, …` -> `Date: Wed, …` if the day-of-week was wrong).
- XML declaration and DOCTYPE blocks are not re-emitted by lxml `tostring`.
- Confluence Storage Format `<ri:user ri:account-id="...">` opaque IDs are not auto-pseudonymized; add them to a denylist if needed.
- Sequential tokens (`--sequential-tokens`) lose cross-document stability; the CLI warns when combined with `--ner`.

[Unreleased]: https://github.com/Hochfrequenz/kuckuck/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Hochfrequenz/kuckuck/releases/tag/v0.1.0
