import unittest

from wsbuilder import (
    build_set_cookie,
    get_cookie,
    get_header,
    has_header,
    normalize_header_name,
    parse_cookie_header,
    set_header,
)


class TestHeadersCookies(unittest.TestCase):
    def test_header_helpers(self):
        headers = {"Content-Type": "text/plain", "X-Test": "1"}
        self.assertEqual(normalize_header_name(" Content-Type "), "content-type")
        self.assertEqual(get_header(headers, "content-type"), "text/plain")
        self.assertTrue(has_header(headers, "x-test"))
        set_header(headers, "X-Test", "2")
        self.assertEqual(get_header(headers, "x-test"), "2")
        set_header(headers, "X-New", "v")
        self.assertEqual(get_header(headers, "x-new"), "v")

    def test_cookie_helpers(self):
        raw = "a=1; b=two; c=3"
        parsed = parse_cookie_header(raw)
        self.assertEqual(parsed["a"], "1")
        self.assertEqual(parsed["b"], "two")

        headers = {"Cookie": raw}
        self.assertEqual(get_cookie(headers, "b"), "two")
        self.assertEqual(get_cookie(headers, "missing", default="x"), "x")

        line = build_set_cookie(
            "sid",
            "abc",
            path="/",
            max_age=60,
            http_only=True,
            same_site="Strict",
        )
        self.assertIn("sid=abc", line)
        self.assertIn("Path=/", line)
        self.assertIn("Max-Age=60", line)
        self.assertIn("HttpOnly", line)
        self.assertIn("SameSite=Strict", line)


if __name__ == "__main__":
    unittest.main()
