import unittest

from verinote.diagnostics.ollama_input_limit import build, corpus


class Measure(unittest.TestCase):
    def test_prompts_hit_the_target_size_and_end_with_the_marker(self):
        for stage in ('extract', 'dictionary'):
            for kind in ('korean', 'ascii'):
                with self.subTest(stage=stage, kind=kind):
                    prompt, _ = build(stage, kind, 8192, 'END-test')
                    size = len(prompt.encode())
                    self.assertLessEqual(size, 8192)
                    self.assertGreater(size, 8192 - 8)
                    self.assertIn('END-test', prompt.splitlines()[-1])

    def test_ascii_corpus_is_ascii(self):
        self.assertTrue(corpus('ascii').isascii())


if __name__ == '__main__':
    unittest.main()
