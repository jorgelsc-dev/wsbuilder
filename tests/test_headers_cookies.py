import unittest

from wsbuilder import (
    Response,
    add_header,
    build_set_cookie,
    get_cookie,
    get_header,
    has_header,
    normalize_header_name,
    parse_cookie_header,
    set_header,
)


class TestHeadersCookies(unittest.TestCase):
    def test_add_header_keeps_existing_values_as_a_list(self):
        headers = {"set-cookie": "a=1"}
        add_header(headers, "Set-Cookie", "b=2")
        add_header(headers, "Set-Cookie", ["c=3", "d=4"])
        add_header(headers, "X-New", "v")
        self.assertEqual(headers, {"set-cookie": ["a=1", "b=2", "c=3", "d=4"], "X-New": "v"})
        with self.assertRaises(ValueError):
            add_header(headers, "Set-Cookie", "bad\r\nInjected: 1")

    def test_response_accepts_list_header_values_and_validates_each(self):
        response = Response.text("ok", headers={"Set-Cookie": ("a=1", "b=2")})
        self.assertEqual(response.headers["Set-Cookie"], ["a=1", "b=2"])
        with self.assertRaises(ValueError):
            Response(headers={"Set-Cookie": ["a=1", "b=2\nX: y"]})
        with self.assertRaises(ValueError):
            Response(headers={"Set-Cookie": []})

    def test_set_header_accepts_list_values(self):
        headers = {}
        set_header(headers, "Set-Cookie", ["a=1", "b=2"])
        self.assertEqual(headers["Set-Cookie"], ["a=1", "b=2"])

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

    def test_headers_and_cookies_reject_response_splitting(self):
        headers = {}
        with self.assertRaises(ValueError):
            set_header(headers, "X-Test\r\nX-Injected", "value")
        with self.assertRaises(ValueError):
            set_header(headers, "X-Test", "value\r\nX-Injected: yes")
        with self.assertRaises(ValueError):
            build_set_cookie("sid", "ok\r\nX-Injected: yes")
        with self.assertRaises(ValueError):
            build_set_cookie("sid", "ok", same_site="sometimes")


if __name__ == "__main__":
    unittest.main()
