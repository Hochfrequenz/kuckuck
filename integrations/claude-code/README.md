# Kuckuck-Hook für Claude Code

Bevor Claude Code eine `.eml`- oder `.msg`-Datei mit `Read`, `Edit` oder `Grep` anfasst, läuft die Datei durch `kuckuck`. Klartext-Adressen, Namen und Telefonnummern sind danach durch `[[EMAIL_…]]`, `[[PERSON_…]]`, `[[PHONE_…]]` ersetzt. Claude sieht nur den pseudonymisierten Inhalt.

Diese Anleitung zeigt den Setup mit dem **Windows-Binary** - ohne Python, ohne pip, ohne venv. Wenn du macOS oder Linux benutzt, siehe die ausklappbaren Abschnitte weiter unten.

## Was du vorher brauchst

- [Claude Code für Windows](https://claude.com/claude-code) installiert (egal ob Anthropic-Installer, npm oder `winget`).
- `winget` (auf Windows 11 vorinstalliert; Windows 10: per Microsoft Store nachinstallieren).
- Ein Projekt-Verzeichnis, in dem du mit Claude Code arbeiten willst.

## Einrichten (5 Schritte, ca. 5 Minuten)

**1. Binary herunterladen** (einmalig pro Rechner):

Lade von der [Releases-Seite](https://github.com/Hochfrequenz/kuckuck/releases/latest) die NER-Variante herunter - die erkennt Personennamen zusätzlich zu Mailadressen und Telefonnummern:

```
kuckuck_windows_ner_<version>.exe    (~ 300 MB)
```

Falls du keine Personenerkennung brauchst, reicht auch die Slim-Variante `kuckuck_windows_<version>.exe` (~ 30 MB).

**2. Binary umbenennen und auf PATH legen** (damit `kuckuck` als Befehl verfügbar ist):

Im PowerShell-Terminal:

```powershell
# Zielordner anlegen
New-Item -ItemType Directory -Path "$env:USERPROFILE\tools\kuckuck" -Force | Out-Null
# Binary rüberschieben und auf "kuckuck.exe" umbenennen
Move-Item -Path "$env:USERPROFILE\Downloads\kuckuck_windows_ner_*.exe" `
          -Destination "$env:USERPROFILE\tools\kuckuck\kuckuck.exe"
# Ordner zur User-PATH hinzufuegen (permanent)
$newPath = [Environment]::GetEnvironmentVariable("Path", "User") + ";$env:USERPROFILE\tools\kuckuck"
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
```

Neues Terminal öffnen, damit der PATH-Eintrag greift. Testen: `kuckuck version` muss eine Versionsnummer ausgeben.

**3. `jq` installieren** (einmalig pro Rechner - der Hook nutzt es, um das JSON von Claude Code zu parsen):

```powershell
winget install jqlang.jq
```

Im neuen Terminal prüfen: `jq --version`.

**4. Master-Key anlegen und Hook installieren** (einmalig pro Projekt):

```powershell
cd C:\pfad\zu\deinem\projekt
kuckuck init-key --project
kuckuck install-claude-hook
```

Erwartete Ausgabe des letzten Befehls:

```
Wrote hook script: C:\pfad\zu\deinem\projekt\.claude\hooks\kuckuck-pseudo.ps1
Updated settings: C:\pfad\zu\deinem\projekt\.claude\settings.json
Restart Claude Code or run /hooks to reload the settings.
```

**5. Claude Code starten** (oder `/hooks` im Chat tippen, falls es schon lief):

```powershell
claude
```

## Verifizieren, dass es wirkt

1. Testdatei im Projekt-Verzeichnis anlegen (PowerShell-Heredoc):

   ```powershell
   @"
   From: Alice <alice@example.com>
   To: Bob <bob@example.com>
   Subject: Test

   Kontakt: klaus.mueller@firma.de, Telefon +49 40 12345-678.
   "@ | Set-Content -Path test.eml
   ```

2. Im Claude-Code-Chat: `Kannst du test.eml lesen?`

3. Was du erwarten solltest:
   - Claude zeigt den Inhalt **mit Tokens statt Klartext**: `Kontakt: [[EMAIL_b1e2…]], Telefon [[PHONE_c3d4…]].`
   - Neu daneben: `test.eml.kuckuck-map.enc` (verschlüsseltes Mapping für den Restore).
   - Im Claude-Code-Terminal: `test.eml -> test.eml (3 replacements, format: eml, map: test.eml.kuckuck-map.enc)`.

4. Zum Aufräumen: `kuckuck restore test.eml; Remove-Item test.eml, test.eml.kuckuck-map.enc`.

Wenn der Chat `[[EMAIL_…]]`-Tokens zeigt und die Sidecar-Datei existiert, wirkt der Hook. Fertig.

Falls etwas nicht klappt: **Troubleshooting** weiter unten.

---

<details>
<summary><strong>Setup auf macOS</strong></summary>

Unter macOS läuft der Binary identisch, nur mit anderer Datei und dem Extra-Schritt, das Gatekeeper-Quarantäne-Attribut zu entfernen.

```bash
# NER-Variante empfohlen (300 MB, erkennt Personennamen)
curl -LO https://github.com/Hochfrequenz/kuckuck/releases/latest/download/kuckuck_macos_arm64_ner_<version>
# Quarantaene entfernen (sonst killt macOS das Binary kommentarlos)
xattr -c kuckuck_macos_arm64_ner_*
# Umbenennen und ausfuehrbar machen
mv kuckuck_macos_arm64_ner_* /usr/local/bin/kuckuck
chmod +x /usr/local/bin/kuckuck

# jq (falls nicht vorhanden)
brew install jq

# Projekt-Setup
cd /pfad/zu/deinem/projekt
kuckuck init-key --project
kuckuck install-claude-hook
```

Dann Claude Code neu starten. Verifikation wie im Windows-Quick-Start, nur mit `cat > test.eml <<'EOF' … EOF` statt dem PowerShell-Heredoc.

</details>

<details>
<summary><strong>Setup auf Linux (Python/pip)</strong></summary>

Für Linux bauen wir keinen Binary. Installation via pip:

```bash
pip install "kuckuck[cli]"
sudo apt install jq
cd /pfad/zu/deinem/projekt
kuckuck init-key --project
kuckuck install-claude-hook
```

</details>

<details>
<summary><strong>Warum zusätzlich zum MCP-Server?</strong></summary>

Der [MCP-Server](../mcp/README.md) stellt Claude das aktive Tool `kuckuck_pseudonymize` zur Verfügung. Damit das schützt, muss das Modell aber daran denken, das Tool *aufzurufen*. Wenn Claude stattdessen einfach `Read mail.eml` aufruft, war es das. Der Hook hier ist die passive Absicherung: er läuft *immer*, auch wenn das Modell nicht darüber nachdenkt. Die beiden Integrationen zusammen heißen Defense-in-Depth - ein Schutz soll weder vergessen (Hook greift) noch umgehen (MCP-Remediation im Fehlerfall) werden können.

Der Hook ist **Claude-Code-spezifisch**. Andere Clients (Cursor, Cline, Zed, opencode, Claude Desktop) haben kein äquivalentes Hook-System; dort trägt nur der MCP-Server.

| | [MCP-Server](../mcp/README.md) | Hook (dieses Dokument) |
|---|---|---|
| Primär-Mechanismus | Modell ruft `kuckuck_pseudonymize(file_path=…)` aktiv auf | Claude Code ruft Script *vor* jedem Read/Edit/Grep |
| Client-Support | client-agnostisch (Claude Code, Cursor, Cline, Zed, opencode, Claude Desktop, …) | nur Claude Code |
| Nutzer-Rolle | Modell muss sich erinnern, das MCP-Tool zu nutzen | passiv, Nutzer muss nichts tun |
| Schutz gegen | bewusst-konventionelle Pseudonymisierung | Vergesslichkeit + direkte Reads ohne Konvention |

Best-Practice: **beide parallel installieren**. Wenn du beides willst, lade auf Windows zusätzlich `kuckuck-mcp_windows_ner_<version>.exe` herunter und folge der [MCP-Anleitung](../mcp/README.md).

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

### Claude zeigt trotzdem Klartext

Wahrscheinlich ist der Hook gar nicht gelaufen. Drei Dinge prüfen:

```powershell
# Claude Code hat die Settings geladen?
Select-String -Path .claude\settings.json -Pattern kuckuck-pseudo
# Hook-Script existiert?
Get-Item .claude\hooks\kuckuck-pseudo.ps1
# kuckuck und jq sind im PATH des Terminals, aus dem Claude Code startet?
kuckuck version; jq --version
```

Claude Code startet den Hook mit dem `PATH`, den das aufrufende Terminal hat. Wenn du Claude Code aus einem Terminal ohne `kuckuck`/`jq` im PATH startest, findet der Hook die Tools nicht.

Abhilfe: `kuckuck.exe` und `jq.exe` müssen entweder in der User-PATH stehen (siehe Schritt 2) oder im gleichen Ordner wie Claude Code.

Wenn Claude Code schon lief, als du den Hook installiert hast: Neustart oder `/hooks` im Chat, damit die Settings neu geladen werden.

### `[kuckuck-hook] kuckuck not found in PATH`

Die Shell, aus der Claude Code den Hook startet, hat `kuckuck` nicht gefunden. Siehe oben.

### `[kuckuck-hook] jq not found in PATH`

`winget install jqlang.jq`, dann Claude Code neustarten.

### `[kuckuck-hook] Refusing to Read … (kuckuck exit 3)`

Kein `.kuckuck-key` gefunden. Einmalig:

```powershell
kuckuck init-key --project      # legt .kuckuck-key im Projekt an
# oder user-global (gilt fuer alle Projekte):
kuckuck init-key
```

### Der Hook blockiert, obwohl gerade nichts zu pseudonymisieren wäre

Der Hook ist bewusst **fail-closed**: im Zweifel blockt er. Wenn du den Schutz für eine Debug-Session abschalten willst:

```powershell
$env:KUCKUCK_HOOK_FAIL_OPEN = "1"
claude
```

Der Hook läuft dann weiter, gibt aber `UNSAFE` auf stderr aus, sobald er in den Fail-Open-Pfad fällt. **Nicht** für Produktions-Sessions gedacht.

</details>

<details>
<summary><strong>Deinstallation</strong></summary>

```powershell
kuckuck install-claude-hook --uninstall
```

Entfernt den Hook-Eintrag aus `.claude/settings.json` und löscht das PowerShell-Script. Andere Hooks bleiben unberührt.

Für die globale Variante:

```powershell
kuckuck install-claude-hook --uninstall --global
```

</details>

<details>
<summary><strong>Globale Installation (für Fortgeschrittene)</strong></summary>

Wenn du den Hook in **jedem** Projekt automatisch aktiv haben willst:

```powershell
kuckuck install-claude-hook --global
```

Das schreibt den Eintrag in `%USERPROFILE%\.claude\settings.json` statt ins Projekt. Vorsicht: der Hook ist **fail-closed**. In Projekten ohne `.kuckuck-key` schlägt jeder `Read(*.eml)` fehl, bis du entweder einen Projekt-Key anlegst oder eine user-globale Key-Datei (`kuckuck init-key`) hast.

Der CLI-Befehl warnt dich auf stderr, wenn du `--global` verwendest.

</details>

<details>
<summary><strong>Was genau in <code>.claude/settings.json</code> landet</strong></summary>

`kuckuck install-claude-hook` schreibt auf Windows:

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
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/kuckuck-pseudo.ps1"
          }
        ]
      }
    ]
  }
}
```

Auf macOS/Linux zeigt der `command`-Eintrag direkt auf die `.sh`-Variante ohne den PowerShell-Prefix.

Zum Nachlesen, was die Felder bedeuten: [Offizielle Claude-Code-Hooks-Doku](https://code.claude.com/docs/en/hooks).

Kurzfassung:

- `matcher: "Read|Edit|Grep"` - nur für diese drei Tools prüfen.
- `if: "Read(*.eml) | …"` - innerhalb dieser drei Tools: nur feuern, wenn der Pfad zu den Glob-Mustern passt. Für jeden anderen Tool-Call ist der Hook unsichtbar.
- `command` - das Script, das bei einem Match vor dem Tool-Call aufgerufen wird.
- `$CLAUDE_PROJECT_DIR` - von Claude Code gesetzte Umgebungsvariable, zeigt auf dein Projekt-Root. Dadurch ist die `settings.json` commit-safe.

Wenn du lieber selbst Hand anlegst statt `install-claude-hook` zu nutzen: siehe `settings.example.json` in diesem Ordner.

</details>

<details>
<summary><strong>Schritt-für-Schritt: was passiert bei einem <code>Read(mail.eml)</code></strong></summary>

1. Claude Code stellt fest: Read-Tool, Pfad `mail.eml`, passt auf `Read(*.eml)`.
2. Claude Code spawnt das Hook-Script mit einer JSON-Payload auf stdin, die den Pfad enthält.
3. Das Script prüft, dass `kuckuck` und `jq` installiert sind.
4. Es extrahiert den Pfad aus `stdin.tool_input.file_path`.
5. Es ruft `kuckuck run mail.eml` auf.
6. Entweder:
   - **Erfolg**: `mail.eml` ist jetzt in-place pseudonymisiert. Script exit 0. Claude Code führt `Read mail.eml` aus und sieht den pseudonymisierten Inhalt.
   - **Fehler** (z. B. kein Key, jq fehlt, kuckuck nicht im PATH): Script exit 2. Claude Code **blockiert** den Tool-Call und zeigt dem Modell die stderr-Ausgabe des Scripts. Die enthält den Hinweis, stattdessen `kuckuck_pseudonymize` über MCP aufzurufen - so lernt Claude den richtigen Workflow statt nur ein "nein" zu sehen.

</details>

<details>
<summary><strong>Grenzen und bewusste Entscheidungen</strong></summary>

- **`Bash cat foo.eml` umgeht den Hook.** Claude-Code-Hooks können keinen zuverlässigen Dateipfad aus einem beliebigen Shell-Command extrahieren (`cat $(find …)`, Subshells, Pipelines). Der Hook fängt nur die drei nativen Tools ab. Die [`AGENTS.md`](../../AGENTS.md) weist Claude an, für `.eml`/`.msg` immer `Read` zu verwenden. Wer das aktiv umgehen will, kann das; der Hook ist Defense-in-Depth, kein Sandbox-Entkommen-Stopper.
- **MCP-Tools anderer Server werden nicht abgefangen.** Der `if`-Filter kennt nur Claude Codes native Tools. Wenn ein anderer MCP-Server eine Datei-Lese-Funktion exportiert, läuft die am Hook vorbei. Deshalb ist `kuckuck-mcp` selbst auf `file_path`-Argumente (nicht `text`-Argumente) festgelegt.
- **`.pst` wird nicht unterstützt.** Kuckuck kennt `.eml`, `.msg`, `.md`, `.xml`, `.html`, Plain-Text. Exchange-Postfächer musst du zuerst in `.eml`-Dateien extrahieren.
- **In-place-Modifikation.** Nach dem Hook ist die Originaldatei pseudonymisiert. `kuckuck restore <file>` ist der Weg zurück. Das ist beabsichtigt - der pseudonymisierte Zustand IST der gewünschte Arbeitszustand.
- **Hook-Timeout 60 s (Claude-Code-Default).** Sehr große `.eml` (>50 MB) können reinlaufen; die CI deckt bis 10 MB ab.
- **Symlinks werden verfolgt.** Ein symbolischer Link `brief.eml -> \secrets\something.txt` würde dazu führen, dass der Hook die Zieldatei pseudonymisiert (falls du Schreibrechte hast). In der Praxis fällt das nicht zufällig vor; wenn du Symlinks im Projekt hast, die auf sensible Dateien zeigen, lass den Hook weg oder prüfe deine Projekt-Struktur.

</details>
