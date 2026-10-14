"""The fallback GTK panel should only be constructed when explicitly opened."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
from recall_app.app import App


class PanelLifecycleTests(unittest.TestCase):
    def test_panel_is_created_once_when_requested(self):
        owner = types.SimpleNamespace(panel=None)
        constructor = Mock()
        module = types.ModuleType('recall_app.panel')
        module.SearchPanel = constructor
        with patch.dict(sys.modules, {'recall_app.panel': module}):
            first = App._search_panel(owner)
            self.assertIs(App._search_panel(owner), first)
        constructor.assert_called_once_with(owner)

    def test_folder_actions_work_without_constructing_a_panel(self):
        owner = types.SimpleNamespace(panel=None, settings=types.SimpleNamespace(onboarded=True),
                                      show_settings=Mock(), settings_window=Mock())
        App.choose_folders(owner)
        owner.show_settings.assert_called_once_with('folders')
        owner.settings_window.add_folders.assert_called_once_with()
        self.assertIsNone(owner.panel)
