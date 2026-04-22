# Kuckuck MCP-Server

Der `kuckuck-mcp` Server exponiert Kuckuck als Set MCP-Tools, sodass MCP-fähige Coding-Assistenten (Claude Desktop, Claude Code, Cursor, Cline, Zed, opencode, ...) Pseudonymisierung direkt aufrufen können — ohne pro-Client-Hook und ohne Konvention via AGENTS.md.

## Installation

Zwei Wege:

**(a) Via pip (empfohlen wenn du Python im Setup hast)**:

```bash
pip install "kuckuck[mcp,ner]"
```

`[mcp]` zieht FastMCP `>=3` und die MCP-Lib, `[ner]` zusätzlich GLiNER+torch für die PERSON-Name-Erkennung.
Nach dem Install ist `kuckuck-mcp` als Console-Script verfügbar:

```bash
which kuckuck-mcp
# /home/you/.local/bin/kuckuck-mcp  oder ähnlich
```

**(b) Als Standalone-Binary** (empfohlen für non-technical User):

Auf der [Releases-Seite](https://github.com/Hochfrequenz/kuckuck/releases/latest) gibt es vier MCP-Binary-Varianten pro Plattform:

| Datei | Größe | Was drin |
|---|---|---|
| `kuckuck-mcp_windows_<ver>.exe` / `kuckuck-mcp_macos_arm64_<ver>` | ~ 43 MB | Slim + MCP. Regex-Detektoren, kein PERSON-Namen. |
| `kuckuck-mcp_windows_ner_<ver>.exe` / `kuckuck-mcp_macos_arm64_ner_<ver>` | ~ 305 MB | NER + MCP. Empfohlen für beste Ergebnisse out-of-the-box. |

Für den MCP-Server brauchst du entweder einen `-mcp`-Binary oder den pip-Install — die "normalen" `kuckuck_*` Binaries (ohne `-mcp`) sind reine CLI.

## Welche Dateien darf der Server anfassen?

Per Default nur Dateien unter dem `$PWD` zum Server-Start.
Bei editor-basierten Clients (Claude Code, opencode) ist das automatisch dein Projekt-Root, weil der Editor den Server-Subprocess von dort startet.
Das `kuckuck_pseudonymize`-Tool ist außerdem mit dem MCP-`destructiveHint` markiert, sodass der Client vor dem Aufruf eine Bestätigung einholen kann.

Wenn du den Bereich erweitern oder anders setzen willst, geht das via `KUCKUCK_MCP_ALLOWED_ROOTS` (Doppelpunkt-getrennte absolute Pfade, oder `*` zum komplett deaktivieren — letzteres nur in Throwaway-Umgebungen).

## Wie der Server an deinen privaten Key kommt

Der MCP-Server ruft intern `kuckuck.config.load_key(None)` auf, also denselben Lookup-Mechanismus wie die `kuckuck`-CLI.
Reihenfolge (höchste → niedrigste Präferenz):

1. **Env-Variable `KUCKUCK_KEY_FILE`** — explizit gesetzt im MCP-Client-Config (siehe Beispiel-Configs unten).
2. **`$PWD/.kuckuck-key`** — Project-lokal im aktuellen Arbeitsverzeichnis des MCP-Server-Prozesses.
3. **`~/.config/kuckuck/key`** — User-global (XDG-konvention).

Der MCP-Server-Prozess wird vom MCP-Client gestartet; das `$PWD` ist also das Working-Directory des Clients, nicht zwingend dein Projekt-Verzeichnis.
Empfehlung für die meisten Setups: setze `KUCKUCK_KEY_FILE` explizit im MCP-Server-Config, dann bist du unabhängig vom Client-Workdir.

Kein Key gefunden, oder unsicher ob der Server überhaupt erkannt wurde?
Frag dein LLM: "Listet bitte die kuckuck-mcp Tools auf und ruft `kuckuck_status` auf."
In Claude Code / opencode kannst du auch direkt `/mcp` (bzw. `/mcps`) eintippen, das listet alle erkannten MCP-Server samt Tools.
`kuckuck_status` liefert eine `problems`-Liste mit konkreten Remediation-Hinweisen — die kann der Assistent dir direkt vorlesen und durcharbeiten.

Erstmal Key anlegen:

```bash
kuckuck init-key                    # ~/.config/kuckuck/key (User-global)
kuckuck init-key --project          # ./.kuckuck-key (Projekt-lokal)
kuckuck init-key --key-file PATH    # eigener Pfad
```

## Verfügbare Tools und Prompts

Der Server stellt fünf Tools (`kuckuck_pseudonymize`, `kuckuck_restore`, `kuckuck_fetch_model`, `kuckuck_list_detectors`, `kuckuck_status`) und vier Prompts (`setup_kuckuck`, `pseudonymize_before_reading`, `diagnose_kuckuck_setup`, `explain_kuckuck_tokens`) bereit.
Jedes Tool/Prompt hat einen Docstring mit Signatur, Parameter-Beschreibung und Gebrauchshinweisen — MCP-Clients (z. B. Claude Code via `/mcp`, opencode via `/mcps`) zeigen diese direkt im Tool-Picker an.
Die Docstrings sind die Source-of-Truth; sie hier nochmal zu duplizieren würde nur verrotten.

Zwei Dinge, die man aus den Docstrings allein nicht sieht:

- **Kein Text-Input.** Pseudonymisierungs-Tools nehmen nur `file_path`, nie direkten Text. Ein `kuckuck_pseudonymize_text(text="Hallo Anna Müller, ...")` würde den Klartext im Tool-Call-Argument transportieren — also im Modell-Kontext, Conversation-Log und Provider-Telemetrie. Mit `file_path` liest der Server direkt vom Filesystem; das LLM sieht den Klartext nie.
- **Elicitation auf `kuckuck_restore` und `kuckuck_fetch_model`.** Beide Tools holen vor dem Ausführen eine explizite User-Bestätigung über den MCP-Client ein — der Server disclosed keine Cleartext-PII und startet keinen Multi-GB-Download still.

## Konfiguration pro Client

Hochfrequenz-intern: Detail-Infos zu MCP-Konfig-Files stehen unter [brain.hochfrequenz.de — KI-Tools / SAP-MCPs](https://brain.hochfrequenz.de/books/ki-tools-bei-hochfrequenz/chapter/sap-mcps).

### Claude Code

Datei (je nach Scope):
- Global: `~/.claude/settings.json`
- Projekt-lokal: `.claude/settings.json` im Repo (commitbar)

```json
{
  "mcpServers": {
    "kuckuck": {
      "command": "kuckuck-mcp",
      "env": {
        "KUCKUCK_KEY_FILE": "/absolute/path/to/.kuckuck-key"
      }
    }
  }
}
```

Reload via `/mcp` oder Restart.
Siehe `claude_code.json` in diesem Ordner.

### opencode

Datei: `opencode.json` projekt-lokal oder `~/.config/opencode/opencode.json` global.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kuckuck": {
      "type": "local",
      "command": ["kuckuck-mcp"],
      "enabled": true,
      "environment": {
        "KUCKUCK_KEY_FILE": "/absolute/path/to/.kuckuck-key"
      }
    }
  }
}
```

Restart opencode.
Siehe `opencode.json` in diesem Ordner.

### Claude Desktop

Datei (je nach OS):
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "kuckuck": {
      "command": "kuckuck-mcp",
      "env": {
        "KUCKUCK_KEY_FILE": "/absolute/path/to/.kuckuck-key"
      }
    }
  }
}
```

Restart Claude Desktop nach dem Editieren.
Siehe `claude_desktop.json` in diesem Ordner als Vorlage.

## Verifikation

1. Starte deinen MCP-Client neu nach dem Editieren der Config.
2. Prüfe ob die Tools sichtbar sind — in Claude Code z. B. `/mcp` oder im Tool-Picker.
3. Lass den Assistenten `kuckuck_status` aufrufen.
   Erwartete Ausgabe:
   ```
   key_found: true
   gliner_installed: false (oder true falls kuckuck[ner] installiert)
   model_available: false (oder true falls fetch-model gelaufen)
   ```
4. Test-Roundtrip: lege eine `test.eml` mit synthetischem Inhalt an, lass den Assistenten `kuckuck_pseudonymize(file_path="test.eml")` aufrufen, prüfe dass die Datei jetzt `[[EMAIL_...]]`-Tokens enthält.

## Troubleshooting

**`kuckuck-mcp: command not found` im MCP-Client**: der MCP-Client startet den Subprocess in seinem eigenen `PATH`. Wenn du `kuckuck-mcp` in einer venv installiert hast, gib den absoluten Pfad an:

```json
{
  "mcpServers": {
    "kuckuck": {
      "command": "/home/you/.venvs/work/bin/kuckuck-mcp"
    }
  }
}
```

**`kuckuck_status` zeigt `key_found: false`**: setze `KUCKUCK_KEY_FILE` im MCP-Server-Block auf einen absoluten Pfad.
Der Server-Subprocess erbt das Working-Directory des Clients, das ist meistens **nicht** dein Repo-Verzeichnis.

**`kuckuck_restore` antwortet immer mit "cancelled"**: dein MCP-Client unterstützt evtl. keine Elicitation oder hat sie nicht konfiguriert.
Claude Desktop und Claude Code unterstützen Elicitation; opencode hatte sie zum Zeitpunkt der Spec-Implementierung noch nicht überall ausgerollt.
Workaround bis dann: nutze `kuckuck restore <file>` lokal in der CLI.

## Wenn du den Hook (#9) auch installierst

Der MCP-Server bietet die aktive Schnittstelle ("Modell ruft `kuckuck_pseudonymize` auf").
Issue [#9](https://github.com/Hochfrequenz/kuckuck/issues/9) ergänzt einen passiven PreToolUse-Hook für Claude Code, der `Read(*.eml)` blockt und auf `kuckuck_pseudonymize` verweist.
Beides zusammen ist Defense-in-Depth: das Modell kann den Schutz weder vergessen (Hook fängt direkten Read) noch umgehen (Hook erzwingt den MCP-Pfad).
