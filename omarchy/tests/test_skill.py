import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_skill
import cli


class SkillTests(unittest.TestCase):
    def test_print_is_embedded_and_does_not_contact_service(self):
        output = io.StringIO()
        with patch.object(cli, 'request') as request, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['skill']), 0)
        self.assertEqual(output.getvalue(), agent_skill.SKILL_TEXT)
        request.assert_not_called()
        source = Path(__file__).resolve().parents[1] / 'skills/recall/SKILL.md'
        self.assertEqual(agent_skill.SKILL_TEXT, source.read_text())

    def test_install_is_idempotent_and_links_existing_identical_codex_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude').mkdir()
            codex = home / '.codex/skills/recall'
            codex.mkdir(parents=True)
            (codex / 'SKILL.md').write_text(agent_skill.SKILL_TEXT)
            with patch.object(agent_skill.Path, 'home', return_value=home), patch.dict(os.environ, CODEX_HOME=str(home / '.codex')):
                agent_skill.install()
                agent_skill.install()
            shared = home / '.agents/skills/recall'
            self.assertEqual((shared / 'SKILL.md').read_text(), agent_skill.SKILL_TEXT)
            self.assertEqual(codex.resolve(), shared)
            self.assertEqual((home / '.claude/skills/recall').resolve(), shared)

    def test_install_preserves_custom_claude_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            existing = home / '.claude/skills/recall'
            existing.mkdir(parents=True)
            (existing / 'SKILL.md').write_text('custom')
            with patch.object(agent_skill.Path, 'home', return_value=home), patch.dict(os.environ, CODEX_HOME=str(home / '.codex')):
                agent_skill.install()
            self.assertEqual((existing / 'SKILL.md').read_text(), 'custom')
