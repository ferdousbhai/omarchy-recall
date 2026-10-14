"""Print/install the skill shipped with the CLI, without needing the service."""
import os
import shutil
from pathlib import Path

from embedded_skill import SKILL_TEXT


def install():
    home = Path.home()
    shared = home / '.agents/skills/recall'
    shared.mkdir(parents=True, exist_ok=True)
    target = shared / 'SKILL.md'
    temporary = shared / '.SKILL.md.tmp'
    temporary.write_text(SKILL_TEXT)
    temporary.replace(target)
    links = []
    # Migrate the identical copy installed by earlier Recall releases.
    codex = Path(os.environ.get('CODEX_HOME', home / '.codex')) / 'skills/recall'
    old = codex / 'SKILL.md'
    if not codex.is_symlink() and old.is_file() and old.read_text() == SKILL_TEXT:
        if set(codex.iterdir()) == {old}:
            old.unlink()
            codex.rmdir()
            codex.symlink_to(shared, target_is_directory=True)
            links.append(str(codex))
    if not codex.exists() and not codex.is_symlink():
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.symlink_to(shared, target_is_directory=True)
        links.append(str(codex))
    # Like HEY, Claude's skill entry points to the canonical shared directory.
    if (home / '.claude').is_dir() or shutil.which('claude'):
        claude = home / '.claude/skills/recall'
        claude.parent.mkdir(parents=True, exist_ok=True)
        if not claude.exists() and not claude.is_symlink():
            claude.symlink_to(shared, target_is_directory=True)
            links.append(str(claude))
    return {'installed': str(target), 'links': links}
