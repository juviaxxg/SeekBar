import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from seekbar.hotkey import DoubleCommand
from seekbar.search import Result, is_navigable_folder, name_score, search, spotlight_paths


class HotkeyTests(unittest.TestCase):
    def test_releases_trigger_once(self):
        hotkey = DoubleCommand()
        events = ((55, True, 1), (55, False, 1.04), (55, True, 1.2), (55, False, 1.24), (55, True, 1.3), (55, False, 1.34))
        self.assertEqual([hotkey.modifier(key, down, False, at) for key, down, at in events], [False, False, False, True, False, False])

    def test_right_command_also_triggers(self):
        hotkey = DoubleCommand()
        for at in (1.0, 1.2):
            self.assertFalse(hotkey.modifier(54, True, False, at))
            self.assertEqual(hotkey.modifier(54, False, False, at + 0.04), at == 1.2)

    def test_control_does_not_trigger(self):
        hotkey = DoubleCommand()
        for key in (59, 62):
            for at in (1.0, 1.2):
                self.assertFalse(hotkey.modifier(key, True, False, at))
                self.assertFalse(hotkey.modifier(key, False, False, at + 0.04))

    def test_chord_hold_and_timeout(self):
        for intervening in (lambda _: None, lambda key: key.reset(), lambda key: key.modifier(60, True, True, 1.1)):
            with self.subTest():
                hotkey = DoubleCommand()
                hotkey.modifier(55, True, False, 1.0)
                hotkey.modifier(55, False, False, 1.02)
                intervening(hotkey)
                hotkey.modifier(55, True, False, 1.65)
                self.assertFalse(hotkey.modifier(55, False, False, 1.67))
        for action in (lambda key: key.reset(), lambda key: key.modifier(60, True, True, 1.1)):
            hotkey = DoubleCommand()
            hotkey.modifier(55, True, False, 1.0)
            hotkey.modifier(55, False, False, 1.02)
            action(hotkey)
            hotkey.modifier(55, True, False, 1.2)
            self.assertFalse(hotkey.modifier(55, False, False, 1.22))
        hotkey = DoubleCommand()
        hotkey.modifier(54, True, False, 1)
        self.assertFalse(hotkey.modifier(54, False, False, 1.45))
        hotkey.modifier(54, True, False, 1.52)
        self.assertFalse(hotkey.modifier(54, False, False, 1.55))


class SearchTests(unittest.TestCase):
    def test_fuzzy_apps_and_ranking(self):
        app = Result('/Applications/Visual Studio Code.app', 'app')
        self.assertEqual(search('vsc', 'app', None, [app], [], []), [app])
        self.assertGreater(name_score('cafe', 'Café'), name_score('cafe', 'c a f e'))

    def test_extension_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / '期末报告.pdf'
            second = root / '期末报告.txt'
            first.touch()
            second.touch()
            with patch('seekbar.search.spotlight_paths', return_value=[str(first), str(second)]) as spotlight:
                result = search('期末 ext:pdf', 'file', folder, [], [], [])
                self.assertEqual(result, [Result(str(first), 'file')])
                self.assertIn('*.pdf', spotlight.call_args.args[0])
                self.assertEqual(spotlight.call_args.kwargs['root'], folder)
            self.assertEqual(search(str(root), 'folder', folder, [], [], []), [Result(folder, 'folder')])

    def test_desktop_fallback_finds_unindexed_chinese_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop = Path(directory) / 'Desktop'
            target = desktop / '开发配置文件' / '开发相关.md'
            target.parent.mkdir(parents=True)
            target.touch()
            (target.parent / '开发相关草稿.md').touch()
            with patch('seekbar.search.spotlight_paths', return_value=[]):
                for scope in ('all', 'file'):
                    with self.subTest(scope=scope):
                        results = search('开发相关', scope, None, [], [], [], local_roots=(str(desktop),))
                        self.assertEqual(results[0], Result(str(target), 'file'))
                self.assertEqual(search('开发相关 ext:md', 'file', str(target.parent), [], [], [])[0].path, str(target))
                self.assertEqual(search('开发相关', 'folder', str(target.parent), [], [], []), [])

    def test_fallback_reports_folder_permission_errors(self):
        inaccessible = set()
        with patch('seekbar.search.spotlight_paths', return_value=[]), patch('seekbar.search.os.scandir', side_effect=PermissionError):
            result = search('开发相关', 'file', None, [], [], [], local_roots=('/desktop',), inaccessible=inaccessible)
        self.assertEqual(result, [])
        self.assertEqual(inaccessible, {'/desktop'})

    def test_folder_scope_excludes_apps_and_their_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'ChatGPT.APP'
            inside = app / 'ChatGPT Resources'
            inside.mkdir(parents=True)
            shortcut = root / 'ChatGPT Link'
            shortcut.symlink_to(app, target_is_directory=True)
            project = root / 'ChatGPT 项目'
            project.mkdir()
            for path in (app, inside, shortcut):
                with self.subTest(path=path):
                    self.assertFalse(is_navigable_folder(str(path)))
                    self.assertEqual(search(str(path), 'folder', None, [], [], []), [])
            self.assertTrue(is_navigable_folder(str(project)))
            app_result = Result(str(app), 'app')
            cached = [Result(str(shortcut), 'folder'), Result(str(project), 'folder')]
            self.assertEqual(search('', 'folder', None, [app_result], [], cached), cached[1:])
            for indexed in ([], [str(app), str(inside), str(shortcut), str(project)]):
                with patch('seekbar.search.spotlight_paths', return_value=indexed):
                    self.assertEqual(search('ChatGPT', 'folder', directory, [app_result], [], []), cached[1:])
            self.assertEqual(search('ChatGPT', 'app', None, [app_result], [], []), [app_result])

    def test_spotlight_subprocess(self):
        files = spotlight_paths('kMDItemFSName == "*"cd', root='/Applications', limit=5)
        self.assertLessEqual(len(files), 5)
        self.assertTrue(all(file.startswith('/Applications/') for file in files))
