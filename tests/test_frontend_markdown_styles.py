from pathlib import Path
import re
import unittest


class FrontendMarkdownStyleTests(unittest.TestCase):
    def test_list_container_does_not_preserve_formatting_newlines(self):
        styles=(Path(__file__).resolve().parents[1]/'frontend/src/styles.css').read_text(encoding='utf-8')
        blocks=re.findall(r'\.markdown li\s*\{([^}]*)\}',styles,re.S)
        self.assertTrue(blocks)
        self.assertFalse(any(re.search(r'white-space\s*:\s*pre-line',block) for block in blocks))
        self.assertTrue(any(re.search(r'white-space\s*:\s*normal',block) for block in blocks))

    def test_nested_list_spacing_is_explicit(self):
        styles=(Path(__file__).resolve().parents[1]/'frontend/src/styles.css').read_text(encoding='utf-8')
        self.assertIn('.markdown li > p',styles)
        self.assertIn('.markdown li > ul',styles)
        self.assertIn('.markdown li + li',styles)


if __name__=='__main__':
    unittest.main()
