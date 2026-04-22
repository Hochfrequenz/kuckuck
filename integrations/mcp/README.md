# Kuckuck MCP-Server

Der `kuckuck-mcp` Server exponiert Kuckuck als Set MCP-Tools, sodass MCP-fähige Coding-Assistenten (Claude Desktop, Claude Code, Cursor, Cline, Zed, opencode, ...) Pseudonymisierung direkt aufrufen können — ohne pro-Client-Hook und ohne Konvention via AGENTS.md.

## Installation

```bash
pip install "kuckuck[mcp]"
```

Das installiert `kuckuck-mcp` als Console-Script und FastMCP `>=3` als Dependency.
Verifiziere die Installation:

```bash
which kuckuck-mcp
# /home/you/.local/bin/kuckuck-mcp  oder ähnlich
```

## Wie der Server an deinen privaten Key kommt

Der MCP-Server ruft intern `kuckuck.config.load_key(None)` auf, also denselben Lookup-Mechanismus wie die `kuckuck`-CLI.
Reihenfolge (höchste → niedrigste Präferenz):

1. **Env-Variable `KUCKUCK_KEY_FILE`** — explizit gesetzt im MCP-Client-Config (siehe Beispiel-Configs unten).
2. **`$PWD/.kuckuck-key`** — Project-lokal im aktuellen Arbeitsverzeichnis des MCP-Server-Prozesses.
3. **`~/.config/kuckuck/key`** — User-global (XDG-konvention).

Der MCP-Server-Prozess wird vom MCP-Client gestartet; das `$PWD` ist also das Working-Directory des Clients, nicht zwingend dein Projekt-Verzeichnis.
Empfehlung für die meisten Setups: setze `KUCKUCK_KEY_FILE` explizit im MCP-Server-Config, dann bist du unabhängig vom Client-Workdir.

Kein Key gefunden? Der `kuckuck_status` MCP-Tool gibt dir eine klare Diagnose:

```json
{
  "key_found": false,
  "key_error": "No Kuckuck key file found. Searched: ...",
  "gliner_installed": true,
  "model_available": false,
  "model_path": "/home/you/.cache/kuckuck/models/gliner_multi-v2.1"
}
```

Erstmal Key anlegen:

```bash
kuckuck init-key                    # ~/.config/kuckuck/key (User-global)
kuckuck init-key --project          # ./.kuckuck-key (Projekt-lokal)
kuckuck init-key --key-file PATH    # eigener Pfad
```

## Verfügbare Tools

| Tool | Was es macht | PII-Leak ins Modell? |
|---|---|---|
| `kuckuck_pseudonymize(file_path, format=auto, ner=false, dry_run=false)` | Pseudonymisiert die Datei in-place, schreibt Mapping-Sidecar daneben | Nein - nur Status-Line ("ok: foo.eml -> 4 replacements") |
| `kuckuck_restore(file_path)` | Restored Klartext aus dem Sidecar-Mapping | Ja - **gated über FastMCP-Elicitation**: User muss aktiv "yes" bestätigen |
| `kuckuck_list_detectors()` | Listet aktive Detektoren (email, phone, handle, term, ner) | Nein |
| `kuckuck_status()` | Self-Diagnose (key found, gliner installiert, model on disk) | Nein |

Alle Tools nehmen einen `file_path`, kein direktes Text-Argument — siehe [Decision 7 in Issue #10](https://github.com/Hochfrequenz/kuckuck/issues/10#issuecomment-4294693864): Text-Tool hätte das LLM den Klartext im Tool-Argument schon sehen lassen, was den Schutz aushebelt.

## Konfiguration pro Client

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

### Cursor

Datei (je nach Scope):
- Global: `~/.cursor/mcp.json`
- Projekt-lokal: `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "kuckuck": {
      "command": "kuckuck-mcp"
    }
  }
}
```

Restart Cursor.
Siehe `cursor.json` in diesem Ordner.

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
Claude Desktop und Claude Code unterstützen Elicitation; Cursor und opencode haben sie zum Zeitpunkt der Spec-Implementierung noch nicht überall ausgerollt.
Workaround bis dann: nutze `kuckuck restore <file>` lokal in der CLI.

## Wenn du den Hook (#9) auch installierst

Der MCP-Server bietet die aktive Schnittstelle ("Modell ruft `kuckuck_pseudonymize` auf").
Issue [#9](https://github.com/Hochfrequenz/kuckuck/issues/9) ergänzt einen passiven PreToolUse-Hook für Claude Code, der `Read(*.eml)` blockt und auf `kuckuck_pseudonymize` verweist.
Beides zusammen ist Defense-in-Depth: das Modell kann den Schutz weder vergessen (Hook fängt direkten Read) noch umgehen (Hook erzwingt den MCP-Pfad).
