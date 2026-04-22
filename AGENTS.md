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
- XML-Parser brauchen `resolve_entities=False, no_network=True, load_dtd=False`.
- HTML-Parser müssen `<script>`/`<style>` strippen, bevor sie `.text()` aufrufen.
- Pickle-loadende Pfade brauchen ein explizites Opt-in-Flag (siehe `--allow-untrusted-model` in `cmd_fetch_model`).

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
