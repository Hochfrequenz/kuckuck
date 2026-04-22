# Agent-Anweisungen für das Kuckuck-Repo

Dieses Dokument richtet sich an KI-Coding-Assistenten (Claude Code, Cursor, Copilot, Codex, …), die in diesem Repository arbeiten.
Es spiegelt die Konventionen wider, die das Kuckuck-Team selbst befolgt.

## Tools, die Agenten in diesem Repo benutzen sollen

Kuckuck dog-foodet sich selbst.
Wenn du als Agent eine Datei mit personenbezogenen Daten in die Hand bekommst (E-Mail, Jira-Export, Confluence-Seite, Chat-Log, …), schicke sie zuerst durch Kuckuck, bevor du sie liest, zitierst oder an ein anderes Modell weiterreichst.

```bash
kuckuck <file>                 # ersetzt die Datei in-place, schreibt verschlüsseltes Mapping daneben
kuckuck restore <file>         # Original wiederherstellen, nachdem die Analyse fertig ist
```

Erkennungs-Token im pseudonymisierten Output:

- `[[EMAIL_...]]` — E-Mail-Adresse
- `[[PHONE_...]]` — Telefonnummer
- `[[HANDLE_...]]` — Jira-/Confluence-Mention
- `[[TERM_...]]` — Eintrag aus `denylist.txt` (Kunden-/Projektnamen)
- `[[PERSON_...]]` — Personenname (nur mit `kuckuck --ner`, vorher einmalig `kuckuck fetch-model`)

Gleiche Originale bekommen den gleichen Token, auch dokumentübergreifend.
Du darfst bei der Analyse annehmen, dass `[[HANDLE_abc]]` mit gleichem Suffix in mehreren Dateien dieselbe Person meint, ohne den Klartext zu kennen.

### Regeln für jede Sitzung

1. Bevor du eine `.eml`-, `.msg`-, exportierte Jira-/Confluence-Datei oder einen Chat-Log liest: `kuckuck <file>` aufrufen.
2. Niemals `*.kuckuck-map.enc` oder `.kuckuck-key` committen — beide sind in `.gitignore`.
3. Tokens in deinen Antworten **nicht** auflösen.
   Der Mensch führt den Restore-Schritt lokal aus.
4. **In Claude Code: für `.eml`/`.msg` immer `Read` benutzen, niemals `Bash cat foo.eml`** (oder `head`, `grep`, Pipelines).
   Der PreToolUse-Hook (`integrations/claude-code/kuckuck-pseudo.sh`) fängt nur native Tools (`Read`, `Edit`, `Grep`) ab, weil `Bash`-Kommandos keinen zuverlässig extrahierbaren Dateipfad haben (`cat $(find …)`, Subshells, Pipelines).
   Wer über `Bash cat` liest, umgeht die Pseudonymisierung - das ist eine dokumentierte Hook-Grenze, kein Feature.

## Konventionen für Code-Änderungen in diesem Repo

### Sprache & Stil

- README, CHANGELOG, RELEASE.md, AGENTS.md und andere User-Doku auf **Deutsch**.
  Ein Satz pro Zeile, keine Umbrüche innerhalb eines Satzes.
- Docstrings, Code-Kommentare und CLI-Output (Hilfetexte, Fehlermeldungen) in **Englisch**.
- Keine Em-Dashes (—) im Code, in CLI-Output oder in Tests — nur ASCII-Hyphen (`-`).
  In Markdown-Doku sind sie OK.
- Keine Zero-Width-Chars in Source-Files; falls unvermeidbar nur via `\uXXXX`-Escape (pylint E2515 blockiert Literale).
- codespell läuft nur über `src/`, nicht über Tests oder Doku.
  Die Ignore-Liste klein halten; keine 2-3-Zeichen-Fragmente hinzufügen.

### Tests, Lint, Types

`tox` orchestriert alles; alle Envs müssen grün sein, bevor du einen PR ready-for-review setzt.

```bash
tox -e tests          # pytest, syrupy, hypothesis
tox -e snapshots      # syrupy --snapshot-update
tox -e linting        # pylint 10/10, pylint-pydantic
tox -e type_check     # mypy --strict (src + unittests)
tox -e coverage       # >= 80 %
tox -e spell_check    # codespell auf src/
```

### Commits & PRs

- Conventional Commits zwingend: `feat(...)`, `fix(...)`, `refactor(...)`, `docs(...)`, `chore(...)`, `test(...)`, `ci(...)`, `style(...)`.
- PR-Titel im selben Format.
- Kleine Commits bevorzugt (Ziel: 8-15 Commits pro PR).
- Jeder Commit sollte lokal grün sein (zumindest `tox -e tests`).
- Niemals selbst mergen: PR ready-for-review setzen, der Nutzer merged.

### Dependencies

- Runtime-Deps in `[project.dependencies]` und CLI-Deps in `[cli]`: `>=`-Minimum, keine Obergrenzen.
- Test/Build/Lint-Deps in `[tests, linting, type_check, coverage, spell_check, formatting, packaging, build_executable]`: **exakt gepinnt**.
- Nach Änderungen an `pyproject.toml`: `tox -e compile_requirements`.
- Nur `pip`, kein `uv`.
- Python 3.14 ist primary target; Libs auf 3.14-Kompat prüfen, bevor du eine Version pinnst.

### Pseudonymisierungs-Pipeline

- Detektoren leben in `src/kuckuck/detectors/`, Preprocessoren in `src/kuckuck/preprocessors/`.
- Neue Detektoren implementieren das `Detector`-Protokoll (`name`, `entity_type`, `priority`, `detect(text) -> list[Span]`).
- Neue Preprocessoren implementieren das `Preprocessor`-Protokoll (`extract(source) -> list[Chunk]`, `reassemble(source, modified) -> str`).
- Cross-Document-Stabilität: das `Mapping` wird durchgereicht, Token-IDs bleiben konsistent, weil HMAC-SHA256 deterministisch ist.

### Sicherheit

Beim Anfassen kryptographie-naher Dateien (`crypto.py`, `mapping.py`, `config.py`, `__main__.py:fetch-model`):

- Keine eigenen Krypto-Primitive einführen.
  Wenn `cryptography` etwas hat, das benutzen.
- Master-Keys nur als `SecretStr`, nie als `str`.
- Subprocesses dürfen keine Env-Variablen mit Secrets erben (siehe `config.py`-Docstring).
- Pickle-loadende Pfade brauchen ein explizites Opt-in-Flag (siehe `--allow-untrusted-model` in `cmd_fetch_model`).

Beim Anfassen der Preprocessoren (`src/kuckuck/preprocessors/`):

- Neue XML-Parser brauchen `resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False` (siehe `xml.py`).
- Neue HTML-Parser müssen `<script>`/`<style>`/`<noscript>` strippen, bevor sie `.text()` aufrufen (siehe `eml.py:_html_to_text`).
- Reassemble-Pfade müssen format-spezifische Strukturen (CDATA, Multipart-Boundaries, YAML-Frontmatter) erhalten - siehe Snapshot-Tests in `unittests/test_preprocessors.py`.

Beim Anfassen des MCP-Servers (`src/kuckuck_mcp/`):

- **Vor jeder Änderung die FastMCP-Doku lesen.**
  Einstieg: https://gofastmcp.com/llms.txt mit dem Index aller Doc-Pages.
  Konkrete Pages je nach Aufgabe:
  - https://gofastmcp.com/servers/tools.md (Decorator-API, Return-Types, `ToolError`)
  - https://gofastmcp.com/servers/elicitation.md (`ctx.elicit`, `match` über `AcceptedElicitation`/`DeclinedElicitation`/`CancelledElicitation`)
  - https://gofastmcp.com/servers/testing.md (in-process Client + `FastMCPTransport`)
  - https://gofastmcp.com/clients/elicitation.md (`elicitation_handler` für Tests)
- Nicht aus dem Gedächtnis raten - die API hat viele Detailregeln (z. B. `response_type=None` ist in v3 deprecated, `Literal` ist gegenüber `list[str]` bevorzugt), die zwischen v2 und v3 nicht-trivial gewechselt sind.
- Errors via `raise ToolError("...")` für user-facing Fehlermeldungen, nicht via Return-String.
- **Tool-Return-Types als pydantic `BaseModel`, nicht als `TypedDict` oder rohes `dict`.**
  FastMCP serialisiert pydantic-Models nativ in das MCP-Tool-Result-JSON-Schema, der Client sieht typisierte Felder mit Descriptions statt opaque Dicts, und der Server validiert seinen Output beim Rausschicken.
  pydantic + FastMCP ist der Standard-Stack für strukturierte Tool-Antworten.

Beim Anfassen der Library-API (`runner.py`, `options.py`):

- `RunOptions` ist `extra='forbid'` - neue Felder explizit ergänzen, nicht via Subclass schmuggeln.
- `run_pseudonymize` muss typer-frei bleiben, damit das `[cli]` Extra wirklich optional ist.
- `progress_writer`-Callable kommt aus User-Land - keine Secrets, keine Klartexte hineingeben (Ausnahme: dry-run gibt bewusst den ganzen pseudonymisierten Text aus, das ist beabsichtigt und dokumentiert).
- `output_dir`-Pfade kommen aus User-Land - kein blindes `mkdir` auf einer absoluten Pfadangabe ohne mindestens einen Sanity-Check, dass der Pfad nicht z. B. `/etc` ist.

## System-Prompt-Snippet für Cloud-LLMs

Wenn du pseudonymisierten Text via API an Claude / GPT / Gemini schickst, ergänze deinen System-Prompt:

> Im Benutzertext erscheinen Platzhalter der Form `[[TYP_hash]]`, z. B. `[[EMAIL_a7f3b2c1]]` oder `[[PERSON_b1e2c3d4]]`.
> Diese Token sind pseudonymisierte personenbezogene Daten.
> **Übernimm sie wörtlich und unverändert** in deine Antwort.
> **Flektiere sie nicht** (kein Genitiv-s anhängen, keine Kleinschreibung, keine Umbenennung).
> Gleiche Token-IDs im gesamten Text beziehen sich auf dieselbe Entität.

## Wo finde ich was?

- `src/kuckuck/__main__.py` — typer-CLI plus `run_pseudonymize()`-Library-API.
- `src/kuckuck/options.py` — `RunOptions`-Modell, das CLI und Library teilen.
- `src/kuckuck/pseudonymize.py` — Detektor-Loop + Mapping-Allokation.
- `src/kuckuck/detectors/` — Regex-Detektoren plus `NerDetector`.
- `src/kuckuck/preprocessors/` — Format-aware Splits für `.eml` / `.msg` / `.md` / `.xml`.
- `src/kuckuck/crypto.py`, `mapping.py`, `config.py` — Mapping-Persistenz und Key-Lookup.
- `unittests/example_files/` — synthetische Fixtures (keine echten PII), commit-bar.
- `RELEASE.md` — Schritt-für-Schritt-Anleitung für ein neues Release.
- `CHANGELOG.md` — Keep-a-Changelog-formatiert, pro Release zu pflegen.
