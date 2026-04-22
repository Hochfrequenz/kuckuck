# Release-Anleitung

Schritt-für-Schritt-Anleitung für ein neues Kuckuck-Release auf PyPI und GitHub.
Die ersten beiden Punkte (PyPI Trusted Publisher, GitHub Environment) sind einmalig zu erledigen, danach reicht pro Release Punkt 3 ff.

## Einmalige Einrichtung

### 1. GitHub-Environment `release` anlegen

1. Im Repo unter **Settings → Environments → New environment**, Name `release`.
2. Optional: Reviewer hinterlegen, die ein Release manuell freigeben müssen, bevor der Publish-Job läuft.
3. Optional: Branch-Schutz so konfigurieren, dass nur Tags vom `main`-Branch deployen dürfen.

### 2. PyPI Trusted Publisher konfigurieren

PyPI verlangt OIDC-Vertrauen statt API-Token, das ist der empfohlene moderne Weg.

1. Auf https://pypi.org/manage/account/publishing/ einloggen mit dem Hochfrequenz-PyPI-Account.
2. Unter **Pending publishers → Add a new pending publisher**:
   - **PyPI Project Name:** `kuckuck`
   - **Owner:** `Hochfrequenz`
   - **Repository name:** `kuckuck`
   - **Workflow name:** `python-publish.yml`
   - **Environment name:** `release`
3. Speichern.
   Sobald der erste Release-Workflow gegen diese Konfiguration läuft, wird der pending publisher zum aktiven Publisher.

### 3. PyPI-Namens-Status

`kuckuck` ist auf PyPI **bereits reserviert** und gehört Hochfrequenz (v0.0.1 wurde am 2026-04-21 als Platzhalter veröffentlicht).
Ein neues Release braucht nur eine höhere Versionsnummer.
Falls der Namens-Eintrag jemals von Dritten übernommen wird, muss der Projektname in `pyproject.toml`, README, AGENTS.md und im PyPI-Trusted-Publisher angepasst werden — derzeit nicht relevant.

## Pro-Release-Schritte

### 1. Release-Branch / Vorbereitung

1. `main` lokal aktualisieren: `git checkout main && git pull`.
2. Sicherstellen, dass alle relevanten PRs gemerged sind und CI auf `main` grün ist:
   ```bash
   gh run list --branch main --limit 5
   ```
3. CHANGELOG aktualisieren:
   - `[Unreleased]`-Sektion in eine konkrete Versions-Sektion umbenennen, Datum eintragen.
   - Neuen leeren `[Unreleased]`-Block oben einfügen.
   - Compare-Link am Ende anpassen.
4. Commit auf `main` (oder Release-Branch + PR + Merge):
   ```bash
   git commit -am "docs(changelog): close v0.1.0"
   ```

### 2. Tag setzen

`hatch-vcs` leitet die Version aus dem Tag ab, deshalb muss der Tag zwingend mit `v` beginnen:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

### 3. GitHub-Release erstellen

```bash
gh release create v0.1.0 \
  --title "v0.1.0" \
  --notes-from-tag \
  --verify-tag
```

Alternativ über die UI: **Releases → Draft a new release**, Tag `v0.1.0` auswählen, Release-Notes aus dem CHANGELOG kopieren, Publish.

### 4. CI-Workflow beobachten

Beim GitHub-Release-Event werden zwei Workflows getriggert:

1. **`Build Executable`** — baut die vier PyInstaller-Binaries (slim Windows + macOS + NER Windows + NER macOS) und hängt sie an das Release an.
2. **`Upload Python Package`** (`python-publish.yml`) — baut sdist + wheel und published gegen den PyPI Trusted Publisher.

Beide laufen ~5 bis 15 Minuten.
Status in **Actions** beobachten:

```bash
gh run watch --workflow=python-publish.yml
gh run watch --workflow=build_executable.yml
```

### 5. Verifikation

Nach erfolgreichem Publish:

```bash
# Auf PyPI sichtbar?
curl -sI https://pypi.org/project/kuckuck/0.1.0/

# In neuer venv installierbar?
python -m venv /tmp/verify && /tmp/verify/bin/pip install kuckuck==0.1.0
/tmp/verify/bin/kuckuck version
```

Binaries auf der Releases-Seite herunterladen und gegen `unittests/smoke_test_exe.py` prüfen (oder einfach `kuckuck_*` lokal aufrufen, `init-key` + `run` end-to-end durchspielen).

## Rollback / Yank

PyPI erlaubt kein "Überschreiben" eines bereits veröffentlichten Versions-Tags.
Bei einem fehlerhaften Release:

1. **Auf PyPI yanken** (Version bleibt sichtbar, aber `pip install kuckuck` zieht sie nicht mehr):
   ```bash
   # via Web-UI auf https://pypi.org/manage/project/kuckuck/releases/
   ```
2. Patch-Release vorbereiten (z. B. `v0.1.1`) mit dem Fix, Schritte 1-4 wiederholen.
3. CHANGELOG-Eintrag für die yank'ed Version: kurze Notiz hinzufügen, warum sie zurückgezogen wurde.

GitHub-Release zusätzlich entfernen oder als "draft" markieren, damit Nutzer keine kaputten Binaries herunterladen.

## Checkliste für die Person, die released

- [ ] CI auf `main` ist auf der Commit-SHA grün, die getaggt wird.
- [ ] CHANGELOG enthält die neue Version mit Datum und einer ehrlichen Zusammenfassung der Änderungen.
- [ ] `pyproject.toml` `requires-python` deckt alle Python-Versionen ab, die in der Test-Matrix laufen.
- [ ] `kuckuck version` hardcodet keine Versionsnummer (sie kommt aus `_kuckuck_version.py` via `hatch-vcs`).
- [ ] Tag wird mit `v`-Prefix gesetzt.
- [ ] PyPI Trusted Publisher zeigt diesen Workflow + dieses Environment an.
- [ ] Nach Publish: PyPI-Page geprüft, eine frische `pip install`-Verifikation durchgespielt, Binaries probehalber gestartet.
