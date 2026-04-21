# Kuckuck

![Unittests](https://github.com/Hochfrequenz/kuckuck/workflows/Unittests/badge.svg)
![Coverage](https://github.com/Hochfrequenz/kuckuck/workflows/Coverage/badge.svg)
![Linting](https://github.com/Hochfrequenz/kuckuck/workflows/Linting/badge.svg)
![Build Executable](https://github.com/Hochfrequenz/kuckuck/workflows/Build%20Executable/badge.svg)

Lokale Pseudonymisierung personenbezogener Daten in Textdateien, **bevor** du sie
an Cloud-LLMs gibst. Inspiriert vom Kuckuck, der seine eigenen Eier in fremde
Nester legt — das Pflegeelternteil merkt nichts: genau das macht dieses Tool mit
Namen, E-Mail-Adressen und Telefonnummern in deinen Dokumenten.

Was Kuckuck **ist**:

- Ein lokales CLI- und Library-Tool in Python. Keine Cloud, keine Telemetrie.
- Ein einfacher Weg, um E-Mails, Jira-Tickets und Confluence-Exporte
  pseudonymisieren zu lassen, bevor du sie an Claude, ChatGPT oder ein anderes
  Cloud-LLM gibst.
- Deterministisch: derselbe Name bekommt — über Dokumente und zwischen
  Teammitgliedern mit gleichem Key — denselben Token.

Was Kuckuck **nicht ist**:

- Keine DSGVO-Anonymisierung im Sinne von Erwägungsgrund 26. Pseudonymisierte
  Daten bleiben personenbezogen. Kläre den Einsatz mit deinem DSB ab, bevor du
  Kuckuck produktiv benutzt.
- Kein Ersatz für Datenminimierung. Kontextuelle Re-Identifikation
  (Rolle + Ort + Datum) ist möglich und nicht Aufgabe des Tools.

## Was wird erkannt?

| Entitätstyp | Erkennung |
|---|---|
| E-Mail-Adressen | Regex |
| Telefonnummern | [`phonenumbers`](https://pypi.org/project/phonenumbers/) (Default-Region: DE) |
| Jira-/Confluence-Handles | Regex — `@user.name`, `[~accountid:...]`, `[~user]` |
| Denylist-Einträge | Kunden-/Projektnamen aus einer Datei |

Personen-Namen via NER folgen in einem späteren Release. Das MVP deckt die
häufigsten Datenquellen (Mail-Signaturen, Jira-Reporter, Confluence-Mentions)
bereits mit den Regex-Detektoren ab.

## Installation

### Als Python-Package

```bash
# Library-Nutzung
pip install kuckuck

# Zusätzlich die CLI installieren
pip install "kuckuck[cli]"
```

### Als Standalone-Binary

Lade dir die plattformspezifische Binary von der
[Releases-Seite](https://github.com/Hochfrequenz/kuckuck/releases):

- `kuckuck_windows_<version>.exe` — Windows x64
- `kuckuck_macos_arm64_<version>` — macOS Apple Silicon

Auf macOS nach dem Download einmalig das Quarantäne-Attribut entfernen:

```bash
xattr -c kuckuck_macos_arm64
chmod +x kuckuck_macos_arm64
```

## Key anlegen

Kuckuck pseudonymisiert mit einem geheimen Master-Key, aus dem HMAC- und
Verschlüsselungs-Subkeys abgeleitet werden. Lege ihn einmalig an:

```bash
kuckuck init-key           # schreibt ~/.config/kuckuck/key (User-scoped)
kuckuck init-key --project # alternativ ein Key pro Projekt: ./.kuckuck-key
```

**Key-Sharing im Team:** Kopiere den Inhalt in euren Passwort-Manager (1Password,
Bitwarden, …) und verteile ihn dort. Mit dem gleichen Key bekommt derselbe Name
bei jedem Teammitglied denselben Token — ihr könnt pseudonymisierte Dokumente
untereinander diskutieren.

**Such-Reihenfolge (höchste → niedrigste Präferenz):**

1. CLI-Flag `--key-file PATH`
2. Env-Var `KUCKUCK_KEY_FILE` (auch aus `.env`)
3. `$PWD/.kuckuck-key`
4. `~/.config/kuckuck/key`

## CLI-Nutzung

**Einfachster Fall — Datei direkt ersetzen:**

```bash
kuckuck brief.txt
# → brief.txt enthält jetzt [[EMAIL_a7f3b2c1]] statt max@firma.de
# → brief.txt.kuckuck-map.enc liegt daneben (verschlüsseltes Mapping)
```

**Rückführung nach LLM-Roundtrip:**

```bash
kuckuck restore brief.txt
# → brief.txt ist wieder original
```

**Batch-Verarbeitung:**

```bash
kuckuck docs/*.md
```

**Ohne Überschreiben (Original bleibt):**

```bash
kuckuck brief.txt --output-dir out/
```

**Vorschau (nichts schreiben):**

```bash
kuckuck brief.txt --dry-run
```

**Mit Denylist für Kunden-/Projektnamen:**

```bash
# denylist.txt — eine Zeile pro Eintrag, # sind Kommentare
echo "Kunde Alpha GmbH" >> denylist.txt
echo "Projekt Zugspitze" >> denylist.txt

kuckuck brief.txt --denylist denylist.txt
```

**Sequenzielle Tokens statt HMAC (LLM-freundlicher, aber nicht cross-doc-stabil):**

```bash
kuckuck brief.txt --sequential-tokens
# → [[EMAIL_1]], [[EMAIL_2]], ... pro Dokument
```

**Mapping inspizieren (für Debugging, gibt Klartext aus):**

```bash
kuckuck inspect brief.txt.kuckuck-map.enc
```

**Alle Subkommandos:**

```
kuckuck <file>...          Pseudonymize (Default)
kuckuck run <file>...      Explizit (identisch zur Default-Form)
kuckuck restore <file>...  Mapping anwenden, Original wiederherstellen
kuckuck init-key           Neuen Master-Key generieren
kuckuck inspect <map>      Verschlüsseltes Mapping als Klartext dumpen
kuckuck list-detectors     Alle registrierten Detektoren zeigen
kuckuck version            Version ausgeben
```

## Binary-Nutzung

Die Binaries verhalten sich identisch zur pip-installierten CLI. Beispiel
Windows PowerShell:

```powershell
.\kuckuck_windows.exe init-key
.\kuckuck_windows.exe brief.txt
.\kuckuck_windows.exe restore brief.txt
```

Beispiel macOS:

```bash
./kuckuck_macos_arm64 init-key
./kuckuck_macos_arm64 brief.txt
./kuckuck_macos_arm64 restore brief.txt
```

## Library-Nutzung

```python
from pathlib import Path
from kuckuck import (
    Mapping,
    build_default_detectors,
    load_default_key,
    load_mapping,
    pseudonymize_text,
    restore_text,
    save_mapping,
)

key = load_default_key()
detectors = build_default_detectors(denylist=["Kunde Alpha GmbH"])

source = Path("brief.eml")
text = source.read_text(encoding="utf-8")

# Bei vorhandenem Mapping merge-reload, sonst leer starten
map_path = source.with_suffix(source.suffix + ".kuckuck-map.enc")
mapping = load_mapping(key, map_path) if map_path.is_file() else Mapping()

result = pseudonymize_text(text, key, detectors, mapping=mapping)
source.write_text(result.text, encoding="utf-8")
save_mapping(key, result.mapping, map_path)

# Später: restore
restored = restore_text(source.read_text(encoding="utf-8"), result.mapping)
```

## Integration mit KI-Assistenten

Wenn du Claude Code, Cursor, GitHub Copilot, Codex oder ähnliche
Coding-Assistenten benutzt, die Dateien in deinem Repo lesen können, kannst du
ihnen beibringen, Dokumente mit personenbezogenen Daten **immer** zuerst durch
Kuckuck zu schicken.

### AGENTS.md / CLAUDE.md Snippet

Füge folgenden Abschnitt in deine `AGENTS.md` oder `CLAUDE.md` ein:

````markdown
## PII-Schutz für E-Mails, Tickets und Wiki-Exporte

Dateien mit personenbezogenen Daten (E-Mails, exportierte Jira-Tickets,
Confluence-Seiten, Chat-Logs) dürfen **nicht im Original** gelesen oder an
externe Modelle gesendet werden. Vor der Analyse immer durch Kuckuck
pseudonymisieren:

```bash
kuckuck <file>                 # ersetzt die Datei in-place
kuckuck restore <file>         # Original wiederherstellen, nachdem die
                               # Analyse abgeschlossen ist
```

Erkennung:
- `[[EMAIL_...]]` — E-Mail-Adresse
- `[[PHONE_...]]` — Telefonnummer
- `[[HANDLE_...]]` — Jira-/Confluence-Mention
- `[[DENYLIST_...]]` — Eintrag aus `denylist.txt` (Kunden/Projekte)

Gleiche Namen bekommen den gleichen Token, auch dokumentübergreifend. Du
(Assistent) darfst bei der Analyse annehmen, dass `[[PERSON_abc]]` und
`[[HANDLE_abc]]` vermutlich dieselbe Person sind — ohne den Klartext zu
benötigen.

Regeln:
1. Vor dem ersten Read einer `.eml` / `.msg` / exportierten Jira-/Confluence-
   Datei: `kuckuck <file>` aufrufen.
2. In Commits niemals die `*.kuckuck-map.enc`-Dateien oder `.kuckuck-key`
   mit-committen (sind in `.gitignore`).
3. Tokens in deinen Antworten **nicht** auflösen — der Nutzer führt den
   Restore-Schritt lokal aus.
````

### System-Prompt für Cloud-LLMs (Direct API)

Wenn du pseudonymisierten Text per API an Claude, GPT oder Gemini schickst,
ergänze deinen System-Prompt:

> Im Benutzertext erscheinen Platzhalter der Form `[[TYP_hash]]`, z.B.
> `[[PERSON_a7f3b2c1]]` oder `[[EMAIL_b1e2c3d4]]`. Diese Token sind
> pseudonymisierte personenbezogene Daten. **Übernimm sie wörtlich und
> unverändert** in deine Antwort. **Flektiere sie nicht** (kein Genitiv-s
> anhängen, keine Kleinschreibung, keine Umbenennung). Gleiche Token-IDs im
> gesamten Text beziehen sich auf dieselbe Entität.

## Erkannte Grenzen

- **Kontextuelle Re-Identifikation:** „Der Geschäftsführer eines
  mittelständischen Bäckereibetriebs in 49716 Meppen" ist praktisch eindeutig —
  Kuckuck ersetzt Namen, nicht Kontexte. Kurze Texte sind sicherer als lange.
- **Seltene Namen / Initialen:** Regex kennt keine Namen — bis zum NER-Release
  (geplant als PR 2) werden Klarnamen nur erkannt, wenn sie in Handles oder der
  Denylist stehen.
- **Formate:** Plain-Text. E-Mails (`.eml`, `.msg`), Markdown und XML werden in
  PR 3 format-aware behandelt — aktuell läuft die Pipeline naiv über den
  Rohtext, Code-Blocks und Attribute können false-positive Ersetzungen bekommen.
- **Linkage-Risiko:** Durch die Cross-Document-Konsistenz kann ein Cloud-LLM-
  Provider — wenn er Logs speichert — Tokens über Sessions verketten. Für
  Anthropic/OpenAI B2B mit ausgeschalteter Trainings-Nutzung in der Praxis
  irrelevant, im eigenen Compliance-Kontext aber evaluieren.

## Development

Das Repo folgt dem Hochfrequenz-Python-Template. Tox orchestriert alle
Entwickler-Workflows:

```bash
tox -e dev          # komplettes Dev-Environment erzeugen
tox -e tests        # Unit- und Integrationstests (pytest + syrupy + hypothesis)
tox -e snapshots    # Snapshots regenerieren (--snapshot-update)
tox -e linting      # pylint (10/10 nötig)
tox -e type_check   # mypy --strict
tox -e coverage     # Coverage-Report (>= 80 %)
tox -e build_executable        # PyInstaller Windows/Linux
tox -e build_executable_macos  # PyInstaller macOS + ad-hoc codesign
```

Die Dev-Environment-Einrichtung und PyCharm/VS-Code-Integration sind im
[Template-README](https://github.com/Hochfrequenz/python_template_repository)
dokumentiert.

## Lizenz

MIT — siehe das LICENSE-File bei Inklusion im Release.
