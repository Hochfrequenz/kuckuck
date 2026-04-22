# Kuckuck PreToolUse-Hook für Claude Code

Claude Code kennt ein Hook-System, das Skripte vor jedem Tool-Call ausführen kann ([offizielle Doku](https://code.claude.com/docs/en/hooks)).
Dieser Hook fängt `Read`, `Edit` und `Grep` auf `*.eml`- und `*.msg`-Dateien ab und jagt sie durch Kuckuck, bevor Claude den Inhalt zu Gesicht bekommt.

Das ist die **Defense-in-Depth-Ebene hinter dem [MCP-Server](../mcp/README.md)**.
Der MCP-Server bietet das aktive `kuckuck_pseudonymize`-Tool; der Hook hier sorgt dafür, dass auch ein direkter `Read(foo.eml)` nicht am Schutz vorbeikommt.
Beides zusammen heißt: das Modell kann den Schutz weder vergessen (Hook blockt direkten Read) noch umgehen (Hook erzwingt den MCP-Pfad als Remediation).

## Installation

Einmalig in deinem Projekt:

```bash
pip install "kuckuck[cli]"      # braucht man eh, wenn man Kuckuck-CLI nutzt
apt install jq                  # bzw. 'brew install jq' auf macOS
kuckuck install-claude-hook     # projekt-lokal in .claude/
```

`kuckuck install-claude-hook` legt das Shell-Script unter `.claude/hooks/kuckuck-pseudo.sh` (bzw. `.ps1` unter Windows) ab und schreibt einen idempotenten Merge-Eintrag in `.claude/settings.json`.
Bestehende Hooks in `settings.json` werden nicht überschrieben; ein zweiter Aufruf von `install-claude-hook` ist ein No-Op.

Globale Installation (lauft in *jedem* Projekt):

```bash
kuckuck install-claude-hook --global
```

Global ist nur sinnvoll, wenn alle deine Projekte denselben `.kuckuck-key` haben - sonst blockt der Hook fail-closed alle `Read`-Aufrufe auf `.eml`/`.msg` in Key-freien Projekten.
Das CLI-Kommando warnt dich entsprechend.

Zum Entfernen:

```bash
kuckuck install-claude-hook --uninstall
kuckuck install-claude-hook --uninstall --global
```

Das Script wird gelöscht, der `settings.json`-Eintrag entfernt.
Andere Hooks bleiben erhalten.

## Was der Hook macht

Der Hook filtert via permission-rule `if`-Ausdruck:

```
Read(*.eml) | Read(*.msg) | Edit(*.eml) | Edit(*.msg) | Grep(*.eml) | Grep(*.msg)
```

Für alle anderen Tool-Calls ist der Hook **komplett ausgeblendet** - Claude Code ruft das Script gar nicht erst auf.

Matched einer dieser Aufrufe, passiert folgendes:

1. Pre-flight: `kuckuck` und `jq` müssen auf `PATH` sein.
   Fehlt eins von beiden, exit 2 mit konkretem Install-Hinweis (fail-closed).
2. Stdin-JSON parsen, Dateipfad aus `.tool_input.file_path` (Read/Edit) oder `.tool_input.path` (Grep) ziehen.
3. `kuckuck run <file>` laufen lassen.
   Kuckuck ist idempotent - bereits pseudonymisierte Tokens bleiben unverändert, ein zweiter Lauf ist nur teuer, nicht falsch.
4. Erfolg → exit 0, der Tool-Call läuft weiter gegen die jetzt pseudonymisierte Datei.
5. Fehler (z. B. fehlender Key) → exit 2, der Tool-Call wird **blockiert**.
   Auf stderr landet eine Remediation-Meldung, die auf das `kuckuck_pseudonymize` MCP-Tool verweist.

## Key-Lookup

Der Hook ruft `kuckuck run <file>` auf, also gilt der normale Kuckuck-Key-Lookup:

1. Env-Variable `KUCKUCK_KEY_FILE`
2. `$PWD/.kuckuck-key` (Projekt-lokal)
3. `~/.config/kuckuck/key` (User-global)

Kein Key → `kuckuck run` wirft `KeyNotFoundError` → Hook blockt fail-closed.
Lösung: `kuckuck init-key --project` (projekt-lokal) oder `kuckuck init-key` (user-global).

## Fail-Closed und der `KUCKUCK_HOOK_FAIL_OPEN`-Override

Default-Verhalten: **fail-closed**.
Wenn irgendwas schiefgeht - fehlendes `kuckuck`, fehlendes `jq`, fehlender Key, Parse-Fehler, Disk-voll - blockt der Hook den Tool-Call und gibt auf stderr eine Meldung aus.

Für lokales Triage oder Debug-Szenarien gibt es einen expliziten Escape-Hatch:

```bash
export KUCKUCK_HOOK_FAIL_OPEN=1
```

Setzt du die Env-Variable, lässt der Hook fehlerhafte Läufe **durchlaufen** (exit 0) statt zu blocken.
Auf stderr steht dann `UNSAFE` und der Original-Fehler, damit du das Szenario nicht übersiehst.
Diese Flag ist ausdrücklich **nicht** für Produktions-Shells gedacht - sie hebelt den Schutz aus.

## Konfiguration pro Scope

Die Install-Varianten, falls du die Datei von Hand pflegen willst:

### Projekt-lokal (`.claude/settings.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Edit|Grep",
        "hooks": [
          {
            "type": "command",
            "if": "Read(*.eml) | Read(*.msg) | Edit(*.eml) | Edit(*.msg) | Grep(*.eml) | Grep(*.msg)",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/kuckuck-pseudo.sh"
          }
        ]
      }
    ]
  }
}
```

Siehe `settings.example.json` in diesem Ordner.

### Global (`~/.claude/settings.json`)

Selber Block, aber mit absolutem Pfad statt `$CLAUDE_PROJECT_DIR`:

```json
"command": "/home/you/.claude/hooks/kuckuck-pseudo.sh"
```

### Windows

Auf Native-Windows (kein WSL) installiert der CLI-Subcommand automatisch die `.ps1`-Variante und prefixt den `command`-String mit `powershell -NoProfile -ExecutionPolicy Bypass -File …`, damit Claude Codes Hook-Executor das Script auch ohne file-association findet.

## Verifikation

1. `.eml`-Testdatei im Projekt anlegen:
   ```bash
   cat > test.eml <<'EOF'
   From: Alice <alice@example.com>
   To: Bob <bob@example.com>

   Kontakt: klaus.mueller@firma.de, +49 40 12345-678
   EOF
   ```
2. Claude Code öffnen und im Chat: `Read test.eml`.
3. Erwartetes Verhalten:
   - Claude Code zeigt `test.eml` mit `[[EMAIL_…]]`-Tokens statt `klaus.mueller@firma.de`.
   - `test.eml.kuckuck-map.enc` liegt jetzt daneben (verschlüsseltes Mapping).
   - Auf dem Terminal steht eine Zeile wie `test.eml -> test.eml (3 replacements, …)`.
4. Zum Aufräumen: `kuckuck restore test.eml` und `rm test.eml test.eml.kuckuck-map.enc`.

Hook läuft nicht?
Prüfe `kuckuck`/`jq` in der Shell, aus der du Claude Code startest (Claude Code erbt den `PATH` dieser Shell):

```bash
command -v kuckuck && command -v jq
```

Fehlt eins, installier es und starte Claude Code neu.

## Troubleshooting

**`[kuckuck-hook] kuckuck not found in PATH`**: Claude Code startet nicht aus der Shell, in der du `pip install kuckuck[cli]` gemacht hast, oder du hast das Script in einer venv installiert, die bei Claude Codes Subprocess nicht aktiv ist.
Lösung: starte Claude Code aus derselben Shell (bzw. mit aktivierter venv), oder installiere Kuckuck system-weit.

**`[kuckuck-hook] jq not found in PATH`**: `apt install jq` / `brew install jq` / `winget install jqlang.jq`, dann Claude Code neustarten.

**`[kuckuck-hook] Refusing to Read … (kuckuck exit 3)`**: kein `.kuckuck-key` gefunden.
Einmalig `kuckuck init-key --project` (projekt-lokal) oder `kuckuck init-key` (user-global).

**Hook läuft, aber Claude sieht die Datei weiterhin im Klartext**: cache-Problem?
Nach einem `Read` pseudonymisiert der Hook die Datei; Claude Codes Dateiansicht zeigt aber evtl. einen zwischengespeicherten Vorzustand, bis der Chat-Turn abgeschlossen ist.
Im Zweifel: `/mcp` → `kuckuck_status` laufen lassen, dann neu lesen.

**`Bash cat foo.eml` umgeht den Hook**: ja, by design - Claude-Code-Hooks haben keinen zuverlässigen Dateipfad-Extrahierungspunkt in `Bash`-Kommandos (`cat $(find …)`, `head -n 20 …`, Pipelines, Subshells, …).
Die [`AGENTS.md`](../../AGENTS.md) weist Claude an, für `.eml`/`.msg`-Dateien immer `Read` statt `Bash cat` zu nutzen.
Wer das aktiv umgehen will, kann das; der Hook ist Defense-in-Depth, kein Sandbox-Entkommen-Stopper.

## Verhältnis zum MCP-Server

Beide Integrationen lösen verschiedene Angles:

| | [MCP-Server](../mcp/README.md) | Hook (dieses Dokument) |
|---|---|---|
| Primär-Mechanismus | Modell ruft `kuckuck_pseudonymize(file_path=…)` aktiv auf | Claude Code ruft Script *vor* jedem Read/Edit/Grep |
| Client-Support | client-agnostisch (Claude Code, Cursor, Cline, Zed, opencode, Claude Desktop, …) | nur Claude Code |
| Nutzer-Rolle | Modell muss sich erinnern, das MCP-Tool zu nutzen | passiv, Nutzer muss nichts tun |
| Schutz gegen | bewusst-konventionelle Pseudonymisierung | Vergesslichkeit + direkte Reads ohne Konvention |

Best-Practice: **beide parallel installieren**.
MCP-Server als Primär-Schnittstelle (Claude lernt via Prompts wie `setup_kuckuck`, dass es `kuckuck_pseudonymize` nutzen soll), Hook als Safety-Net, falls das Modell den Umweg über MCP vergisst.

## Grenzen

- **`Bash` wird nicht abgefangen**.
  `cat`, `head`, `grep`, Pipelines ohne explizites `file_path`-Argument bleiben unberührt.
- **MCP-Tools, die Dateien lesen, werden nicht abgefangen**.
  Der `if`-Filter kennt nur Claude Codes native Tools; der Hook kann nicht MCP-Calls inspizieren.
  Das ist die zweite Rolle des MCP-Servers: sich selbst auf `file_path`-Argumente statt `text`-Argumente festlegen und damit nicht versehentlich Klartext durchreichen.
- **Hook-Timeout** (Claude-Code-Default 60 s): sehr große `.eml` (>50 MB) können reinlaufen.
  Die CI deckt bis 10 MB ab.
- **`.pst` nicht unterstützt**.
  Kuckuck kennt `.eml`, `.msg`, `.md`, `.xml`, `.html`, Plain-Text.
- **In-place-Modifikation**.
  Nach dem Hook ist die Originaldatei pseudonymisiert; `kuckuck restore <file>` ist der Weg zurück.
  Das ist Absicht - der pseudonymisierte Zustand IST der gewünschte Arbeitszustand, kein Zwischenschritt.
