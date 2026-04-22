# Kuckuck-Hook für Claude Code

## Was dieses Dokument beantwortet

- Was ist ein Claude-Code-Hook und was tut dieser hier?
- Wie installiere ich ihn in drei Befehlen?
- Woran erkenne ich, dass er wirkt?
- Was mache ich, wenn er streikt?

## Was der Hook tut (in einem Satz)

Bevor Claude Code eine `.eml`- oder `.msg`-Datei mit `Read`, `Edit` oder `Grep` anfasst, läuft die Datei durch `kuckuck run`. Klartext-Adressen, Namen und Telefonnummern sind danach durch `[[EMAIL_…]]`, `[[PERSON_…]]`, `[[PHONE_…]]` ersetzt. Claude sieht nur den pseudonymisierten Inhalt.

## Warum das zusätzlich zum MCP-Server nötig ist

Der [MCP-Server](../mcp/README.md) stellt Claude das aktive Tool `kuckuck_pseudonymize` zur Verfügung. Damit das schützt, muss das Modell aber daran denken, das Tool *aufzurufen*. Wenn Claude stattdessen einfach `Read mail.eml` aufruft, war es das. Der Hook hier ist die passive Absicherung: er läuft *immer*, auch wenn das Modell nicht darüber nachdenkt. Die beiden Integrationen zusammen heißen Defense-in-Depth - ein Schutz soll weder vergessen (Hook greift) noch umgehen (MCP-Remediation im Fehlerfall) werden können.

Der Hook ist **Claude-Code-spezifisch**. Andere Clients (Cursor, Cline, Zed, opencode, Claude Desktop) haben kein äquivalentes Hook-System; dort trägt nur der MCP-Server.

## Quick-Start (3 Minuten)

Voraussetzung: du hast ein Kuckuck-Projekt mit `.kuckuck-key` oder eine user-globale Key-Datei. Wenn nicht, erst `kuckuck init-key --project` in deinem Projekt laufen lassen.

```bash
pip install "kuckuck[cli]"          # falls noch nicht geschehen
sudo apt install jq                 # Linux;   macOS: 'brew install jq'; Windows: 'winget install jqlang.jq'
cd /pfad/zu/deinem/projekt
kuckuck install-claude-hook
```

Erwartete Ausgabe:

```
Wrote hook script: /pfad/zu/deinem/projekt/.claude/hooks/kuckuck-pseudo.sh
Updated settings: /pfad/zu/deinem/projekt/.claude/settings.json
Restart Claude Code or run /hooks to reload the settings.
```

Was der Befehl gemacht hat:

- `.claude/hooks/kuckuck-pseudo.sh` (bzw. `.ps1` auf Windows) angelegt.
  Das ist das Shell-Script, das vor jedem Read/Edit/Grep auf `*.eml`/`*.msg` läuft.
- `.claude/settings.json` um einen `PreToolUse`-Hook-Eintrag erweitert.
  Bestehende Hooks bleiben intakt; ein zweiter Aufruf ist ein No-Op.

Starte Claude Code jetzt neu (oder tippe `/hooks` im Chat, um die Settings ohne Neustart neu zu lesen).

## Überprüfen, dass es wirkt

1. Lege eine Testdatei an:

   ```bash
   cat > test.eml <<'EOF'
   From: Alice <alice@example.com>
   To: Bob <bob@example.com>
   Subject: Test

   Kontakt: klaus.mueller@firma.de, Telefon +49 40 12345-678.
   EOF
   ```

2. Frag Claude Code im Chat: `Kannst du test.eml lesen?`.

3. Was du erwarten solltest:

   - Claude Code zeigt dir den Inhalt **mit Tokens statt Klartext**:

     ```
     From: Alice <[[EMAIL_a7f3…]]>
     ...
     Kontakt: [[EMAIL_b1e2…]], Telefon [[PHONE_c3d4…]].
     ```

   - Im Projekt-Verzeichnis liegt jetzt `test.eml.kuckuck-map.enc` (verschlüsseltes Mapping für den Restore).

   - Im Claude-Code-Terminal (nicht im Chat) siehst du eine Zeile wie

     ```
     test.eml -> test.eml (3 replacements, format: eml, map: test.eml.kuckuck-map.enc)
     ```

     Das ist die Fortschrittsausgabe von `kuckuck run`, über stderr an Claude Codes Konsole weitergereicht.

4. Zum Aufräumen:

   ```bash
   kuckuck restore test.eml
   rm test.eml.kuckuck-map.enc
   rm test.eml
   ```

Wenn dein Chat die Tokens `[[EMAIL_…]]` enthält und die Sidecar-Datei da ist, wirkt der Hook wie beabsichtigt.

## Häufige Probleme beim ersten Versuch

### Claude zeigt trotzdem Klartext

Wahrscheinlich ist der Hook gar nicht gelaufen. Prüfe drei Dinge:

```bash
# Claude Code hat die Settings geladen?
cat .claude/settings.json | grep kuckuck-pseudo
# Hook-Script ist ausführbar?
ls -la .claude/hooks/kuckuck-pseudo.sh
# kuckuck und jq sind im PATH, aus dem Claude Code läuft?
command -v kuckuck && command -v jq
```

Startest du Claude Code aus einer anderen Shell (ohne die Projekt-venv), sieht es weder `kuckuck` noch `jq`. Lösungen:

- Starte Claude Code aus derselben Shell, in der `pip install kuckuck[cli]` gelaufen ist.
- Oder installiere Kuckuck systemweit: `pipx install kuckuck[cli]`.

Wenn Claude Code schon lief, als du den Hook installiert hast: Neustart oder `/hooks` im Chat, damit die Settings neu geladen werden.

### `[kuckuck-hook] kuckuck not found in PATH`

Die Shell, aus der Claude Code den Hook startet, hat `kuckuck` nicht gefunden. Siehe oben.

### `[kuckuck-hook] jq not found in PATH`

`sudo apt install jq` (Linux), `brew install jq` (macOS), `winget install jqlang.jq` (Windows). Dann Claude Code neustarten.

### `[kuckuck-hook] Refusing to Read … (kuckuck exit 3)`

Kein `.kuckuck-key` gefunden. Einmalig:

```bash
kuckuck init-key --project      # legt .kuckuck-key im Projekt an
# oder
kuckuck init-key                # legt ~/.config/kuckuck/key an (gilt für alle Projekte)
```

### Der Hook blockiert, obwohl gerade nichts zu pseudonymisieren wäre

Der Hook ist bewusst **fail-closed**: im Zweifel blockt er. Wenn du den Schutz für eine Debug-Session abschalten willst, starte Claude Code mit:

```bash
KUCKUCK_HOOK_FAIL_OPEN=1 claude
```

Der Hook läuft dann weiter, gibt aber auf stderr `UNSAFE` aus, sobald er in den Fail-Open-Pfad fällt. **Nicht** für Produktions-Sessions gedacht.

## Deinstallation

```bash
kuckuck install-claude-hook --uninstall
```

Das entfernt den Hook-Eintrag aus `.claude/settings.json` und löscht das Shell-Script. Andere Hooks, die du selbst eingetragen hast, bleiben unberührt.

Für die globale Variante:

```bash
kuckuck install-claude-hook --uninstall --global
```

## Globale Installation (für Fortgeschrittene)

Wenn du den Hook in **jedem** Projekt auf deinem Rechner automatisch aktiv haben willst:

```bash
kuckuck install-claude-hook --global
```

Das schreibt den Eintrag in `~/.claude/settings.json` statt ins Projekt. Vorsicht: der Hook ist **fail-closed**. In Projekten ohne `.kuckuck-key` schlägt jeder `Read(*.eml)` fehl, bis du entweder einen Projekt-Key anlegst oder eine user-globale Key-Datei (`kuckuck init-key`) hast.

Der CLI-Befehl warnt dich auf stderr, wenn du `--global` verwendest.

## Was genau in `.claude/settings.json` landet

`kuckuck install-claude-hook` schreibt (projekt-lokal):

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

Zum Nachlesen, was die Felder bedeuten: [Offizielle Claude-Code-Hooks-Doku](https://code.claude.com/docs/en/hooks).

Kurzfassung:

- `matcher: "Read|Edit|Grep"` - nur für diese drei Tools prüfen.
- `if: "Read(*.eml) | ..."` - innerhalb dieser drei Tools: nur feuern, wenn der Pfad zu den Glob-Mustern passt. Für jeden anderen Tool-Call ist der Hook unsichtbar.
- `command` - das Script, das bei einem Match vor dem Tool-Call aufgerufen wird.
- `$CLAUDE_PROJECT_DIR` - von Claude Code gesetzte Umgebungsvariable, zeigt auf dein Projekt-Root. Dadurch ist die `settings.json` commit-safe.

Wenn du lieber selbst Hand anlegst statt `install-claude-hook` zu nutzen: siehe `settings.example.json` in diesem Ordner.

## Windows-Besonderheiten

Auf native Windows (ohne WSL) installiert `kuckuck install-claude-hook` automatisch die `.ps1`-Variante statt der `.sh`. Der `command`-Eintrag in `settings.json` ruft `powershell -NoProfile -ExecutionPolicy Bypass -File …` auf, damit das Script unabhängig von File-Associations läuft. Du brauchst dafür kein zusätzliches Tooling.

`jq` ist auf Windows ebenfalls erforderlich; `winget install jqlang.jq` oder direkt von [github.com/jqlang/jq/releases](https://github.com/jqlang/jq/releases).

## Schritt-für-Schritt: was passiert bei einem `Read(mail.eml)`

1. Claude Code stellt fest: Read-Tool, Pfad `mail.eml`, passt auf `Read(*.eml)`.
2. Claude Code spawnt das Hook-Script mit einer JSON-Payload auf stdin, die den Pfad enthält.
3. Das Script prüft, dass `kuckuck` und `jq` installiert sind.
4. Es extrahiert den Pfad aus `stdin.tool_input.file_path`.
5. Es ruft `kuckuck run mail.eml` auf.
6. Entweder:
   - **Erfolg**: `mail.eml` ist jetzt in-place pseudonymisiert. Script exit 0. Claude Code führt `Read mail.eml` aus und sieht den pseudonymisierten Inhalt.
   - **Fehler** (z. B. kein Key, jq fehlt, kuckuck nicht im PATH): Script exit 2. Claude Code **blockiert** den Tool-Call und zeigt dem Modell die stderr-Ausgabe des Scripts. Die enthält den Hinweis, stattdessen `kuckuck_pseudonymize` über MCP aufzurufen - so lernt Claude den richtigen Workflow statt nur ein "nein" zu sehen.

## Grenzen und bewusste Entscheidungen

- **`Bash cat foo.eml` umgeht den Hook.** Claude-Code-Hooks können keinen zuverlässigen Dateipfad aus einem beliebigen Shell-Command extrahieren (`cat $(find …)`, Subshells, Pipelines). Der Hook fängt nur die drei nativen Tools ab. Die [`AGENTS.md`](../../AGENTS.md) weist Claude an, für `.eml`/`.msg` immer `Read` zu verwenden. Wer das aktiv umgehen will, kann das; der Hook ist Defense-in-Depth, kein Sandbox-Entkommen-Stopper.
- **MCP-Tools anderer Server werden nicht abgefangen.** Der `if`-Filter kennt nur Claude Codes native Tools. Wenn ein anderer MCP-Server eine Datei-Lese-Funktion exportiert, läuft die am Hook vorbei. Deshalb ist `kuckuck-mcp` selbst auf `file_path`-Argumente (nicht `text`-Argumente) festgelegt.
- **`.pst` wird nicht unterstützt.** Kuckuck kennt `.eml`, `.msg`, `.md`, `.xml`, `.html`, Plain-Text. Exchange-Postfächer musst du zuerst in `.eml`-Dateien extrahieren.
- **In-place-Modifikation.** Nach dem Hook ist die Originaldatei pseudonymisiert. `kuckuck restore <file>` ist der Weg zurück. Das ist beabsichtigt - der pseudonymisierte Zustand IST der gewünschte Arbeitszustand.
- **Hook-Timeout 60 s (Claude-Code-Default).** Sehr große `.eml` (>50 MB) können reinlaufen; die CI deckt bis 10 MB ab.
- **Symlinks werden verfolgt.** Ein symbolischer Link `brief.eml -> /etc/passwd` würde dazu führen, dass der Hook die Zieldatei pseudonymisiert (falls du Schreibrechte hast). In der Praxis fällt das nicht zufällig vor; wenn du Symlinks im Projekt hast, die auf sensible Dateien zeigen, lass den Hook weg oder prüfe deine Projekt-Struktur.

## Verhältnis zum MCP-Server

| | [MCP-Server](../mcp/README.md) | Hook (dieses Dokument) |
|---|---|---|
| Primär-Mechanismus | Modell ruft `kuckuck_pseudonymize(file_path=…)` aktiv auf | Claude Code ruft Script *vor* jedem Read/Edit/Grep |
| Client-Support | client-agnostisch (Claude Code, Cursor, Cline, Zed, opencode, Claude Desktop, …) | nur Claude Code |
| Nutzer-Rolle | Modell muss sich erinnern, das MCP-Tool zu nutzen | passiv, Nutzer muss nichts tun |
| Schutz gegen | bewusst-konventionelle Pseudonymisierung | Vergesslichkeit + direkte Reads ohne Konvention |

Best-Practice: **beide parallel installieren**. MCP-Server als Primär-Schnittstelle (Claude lernt via Prompts wie `setup_kuckuck`, dass es `kuckuck_pseudonymize` nutzen soll), Hook als Safety-Net, falls das Modell den Umweg über MCP vergisst.
