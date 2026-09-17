"""Placeholder substitution itself. These pass before and after."""

import unittest

from tmpl.escaping import escape_html
from tmpl.render import render


class Substitution(unittest.TestCase):
    def test_a_placeholder_is_replaced(self):
        self.assertEqual(render("Hello {{name}}!", {"name": "World"}), "Hello World!")

    def test_several_placeholders_are_replaced(self):
        self.assertEqual(
            render("{{a}} and {{b}}", {"a": "one", "b": "two"}), "one and two"
        )

    def test_the_same_placeholder_twice(self):
        self.assertEqual(render("{{x}}-{{x}}", {"x": "9"}), "9-9")

    def test_a_template_without_placeholders_is_unchanged(self):
        self.assertEqual(render("plain text", {}), "plain text")

    def test_a_non_string_value_is_rendered(self):
        self.assertEqual(render("n={{n}}", {"n": 42}), "n=42")


class TheTemplateItselfIsTrusted(unittest.TestCase):
    def test_markup_written_in_the_template_survives(self):
        self.assertEqual(
            render("<b>bold</b> {{x}}", {"x": "y"}),
            "<b>bold</b> y",
        )

    def test_an_attribute_written_in_the_template_survives(self):
        self.assertEqual(
            render('<a href="/p">{{label}}</a>', {"label": "go"}),
            '<a href="/p">go</a>',
        )


class MissingValues(unittest.TestCase):
    def test_a_placeholder_with_no_value_is_a_key_error(self):
        with self.assertRaises(KeyError):
            render("{{missing}}", {"other": 1})


class PlainText(unittest.TestCase):
    def test_escaping_leaves_ordinary_text_alone(self):
        self.assertEqual(escape_html("just words"), "just words")


if __name__ == "__main__":
    unittest.main()
