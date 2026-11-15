import unittest

from prepare_world_float import convert


class FloatRewriteTests(unittest.TestCase):
    def test_only_numeric_code_changes(self):
        original = '''// double cos(0.1)\n/* double 2.3 */
const char* name = "double sqrt(1.0)";
double mydouble = cos(0.1) + sqrt(4) + pow(.5, 2.0) + 1e-20;
int mask = 0xdeadbeef;
float same = 1.5f + 2e-3F;
'''
        expected = '''// double cos(0.1)\n/* double 2.3 */
const char* name = "double sqrt(1.0)";
float mydouble = cosf(0.1f) + sqrtf(4) + powf(.5f, 2.0f) + 1e-20f;
int mask = 0xdeadbeef;
float same = 1.5f + 2e-3F;
'''
        self.assertEqual(convert(original), expected)
        self.assertEqual(convert(expected), expected)


if __name__ == '__main__':
    unittest.main()
