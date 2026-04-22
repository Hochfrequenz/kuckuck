# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **MCP server** `kuckuck-mcp` (Issue [#10](https://github.com/Hochfrequenz/kuckuck/issues/10)).
  Sub-package `src/kuckuck_mcp/`, optional extra `kuckuck[mcp]`, console-script `kuckuck-mcp`.
  Built on FastMCP `>= 3` (consistent with the Hochfrequenz MCP stack).
  Four stdio tools: `kuckuck_pseudonymize`, `kuckuck_restore` (gated behind FastMCP elicitation), `kuckuck_list_detectors`, `kuckuck_status` (with aggregated `problems`-list and remediation hints, pattern from `Hochfrequenz/sap-mcp-config`).
  Three discoverability prompts (`pseudonymize_before_reading`, `diagnose_kuckuck_setup`, `explain_kuckuck_tokens`) so MCP clients surface the safe workflows as quick-actions.
  All tools are `file_path`-based — a text-input variant would have leaked PII through the tool argument.
  No MCP resources exposed (mapping inspection stays local via `kuckuck inspect`).
- Setup guides plus example configs for Claude Desktop, Claude Code, Cursor and opencode in `integrations/mcp/`.
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
