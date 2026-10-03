"""Exercise the homepage selection without importing the live Flask app or database."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class NovelHomeRecentTests(unittest.TestCase):
    def render_home(self, novels, progresses):
        source = Path(__file__).resolve().parents[1] / 'mangadock/blueprints/novel_routes.py'
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {'_novel_with_progress', 'novel_index'}]
        for node in functions:
            node.decorator_list = []
        lookup = Mock(return_value=progresses)
        namespace = {
            'get_novels': lambda: novels,
            '_novel_progress_lookup': lookup,
            'session': {'user_id': 42},
            'get_current_user': lambda: None,
            'render_template': lambda template, **context: context,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
        result = namespace['novel_index']()
        lookup.assert_called_once_with(42)
        return result

    def progress(self, minutes):
        return SimpleNamespace(last_chapter=2, last_read_at=datetime(2026, 10, 2) + timedelta(minutes=minutes))

    def test_reading_order_is_independent_of_library_import_order(self):
        novels = [{'novel_id': 'new'}, {'novel_id': 'old'}, {'novel_id': 'unread'}]
        result = self.render_home(novels, {'new': self.progress(1), 'old': self.progress(10)})
        self.assertEqual([item['novel_id'] for item in result['recent_novels']], ['old', 'new'])
        self.assertEqual(result['spotlight']['novel_id'], 'old')
        self.assertEqual(result['recent_novels'][0]['last_chapter'], 2)

    def test_missing_books_are_skipped_before_limiting_the_rail(self):
        novels = [{'novel_id': str(i)} for i in range(25)]
        progresses = {str(i): self.progress(i) for i in range(25)}
        progresses['deleted'] = self.progress(100)
        result = self.render_home(novels, progresses)
        self.assertEqual(len(result['recent_novels']), 18)
        self.assertEqual([item['novel_id'] for item in result['recent_novels']],
                         [str(i) for i in range(24, 6, -1)])

    def test_unread_library_is_not_presented_as_recent_reading(self):
        result = self.render_home([{'novel_id': 'first'}], {})
        self.assertEqual(result['recent_novels'], [])
        self.assertEqual(result['spotlight']['novel_id'], 'first')
        self.assertIsNone(result['spotlight']['progress'])

    def test_empty_library_has_no_spotlight_or_reading_rail(self):
        result = self.render_home([], {'deleted': self.progress(10)})
        self.assertIsNone(result['spotlight'])
        self.assertEqual(result['recent_novels'], [])


if __name__ == '__main__':
    unittest.main()
