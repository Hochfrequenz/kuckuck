"""Bundled hook scripts for coding-assistant integrations.

The shell and PowerShell scripts in this directory are byte-for-byte
copies of the canonical files under ``integrations/claude-code/``. They
ship inside the wheel so that ``kuckuck install-claude-hook`` can copy
them out at runtime via :mod:`importlib.resources` without depending on
the source checkout layout.

``unittests/test_install_claude_hook.py`` asserts the two copies stay
in sync. Edit ``integrations/claude-code/`` first, then mirror the
change here (or vice versa).
"""
