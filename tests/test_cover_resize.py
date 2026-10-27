"""Check exact resizing without importing the live app or running an AI model."""
import ast
from pathlib import Path
import unittest

from PIL import Image


class CoverResizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'mangadock/utils/cover_enhance.py'
        tree = ast.parse(source.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {'_resample_filter', '_stepwise_resize'}]
        cls.namespace = {'Image': Image}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), cls.namespace)

    def test_ai_output_is_reduced_to_requested_size(self):
        with Image.new('RGB', (2400, 3200)) as image:
            result = self.namespace['_stepwise_resize'](image, (1440, 1920))
            self.assertEqual(result.size, (1440, 1920))

    def test_small_adjustments_also_reach_exact_size(self):
        with Image.new('RGB', (100, 150)) as image:
            result = self.namespace['_stepwise_resize'](image, (101, 151))
            self.assertEqual(result.size, (101, 151))

    def test_stepwise_upscale_still_reaches_requested_size(self):
        with Image.new('RGB', (20, 30)) as image:
            result = self.namespace['_stepwise_resize'](image, (100, 150))
            self.assertEqual(result.size, (100, 150))


if __name__ == '__main__':
    unittest.main()
