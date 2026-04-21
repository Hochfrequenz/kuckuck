"""End-to-end smoke test for the NER-enabled Kuckuck binary.

Companion to :file:`smoke_test_exe.py` — only invoked for the ``*_ner``
matrix entries. Verifies that:

1. ``kuckuck list-detectors`` lists the ``ner`` row when the model is
   present (proves both the import-side and the on-disk model path work).
2. ``kuckuck run --ner <doc>`` produces a ``[[PERSON_...]]`` token for a
   German signature line.

The test assumes the GitHub Actions cache step has already populated
``~/.cache/kuckuck/models/gliner_multi-v2.1/`` from the
``actions/cache@v4`` restore. The script does NOT download the model
itself — that is done once by the dedicated ``fetch_ner_model`` job and
shared across the matrix.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(
    binary: str,
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(f"command failed: {binary} {' '.join(args)}", file=sys.stderr)
        print("stdout:", result.stdout, file=sys.stderr)
        print("stderr:", result.stderr, file=sys.stderr)
    return result


def _model_present() -> bool:
    cache_root = Path(os.path.expanduser("~/.cache/kuckuck/models/gliner_multi-v2.1"))
    if not cache_root.is_dir():
        return False
    return (cache_root / "gliner_config.json").is_file() or (cache_root / "config.json").is_file()


def main(binary: str) -> int:  # pylint: disable=too-many-return-statements
    binary = os.path.abspath(binary)
    if not Path(binary).is_file():
        print(f"binary not found: {binary}", file=sys.stderr)
        return 1

    if not _model_present():
        print("GLiNER model is not in the local cache; skipping NER smoke test.")
        return 0

    listing = _run(binary, ["list-detectors"])
    if listing.returncode != 0 or "ner" not in listing.stdout:
        print("list-detectors did not include the ner detector", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as workspace:
        key = Path(workspace) / "key"
        init = _run(binary, ["init-key", "--key-file", str(key)])
        if init.returncode != 0 or not key.is_file():
            print("init-key failed", file=sys.stderr)
            return 1

        doc = Path(workspace) / "doc.txt"
        doc.write_text(
            "Mit freundlichen Gruessen,\nMax Mustermann\nProjektleiter\n",
            encoding="utf-8",
        )

        run = _run(binary, [str(doc), "--key-file", str(key), "--ner"])
        if run.returncode != 0:
            print("run --ner failed", file=sys.stderr)
            return 1

        output = doc.read_text(encoding="utf-8")
        if "[[PERSON_" not in output:
            print("missing PERSON token in NER output", file=sys.stderr)
            print(output, file=sys.stderr)
            return 1
        if "Max Mustermann" in output:
            print("original name still present in NER output", file=sys.stderr)
            return 1

    print("NER smoke test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <binary>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
