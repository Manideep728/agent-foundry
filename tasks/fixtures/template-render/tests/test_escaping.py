"""Escaping of substituted values. These fail before the fix."""

import unittest

from tmpl.escaping import escape_html
from tmpl.render import render


class EscapeHtml(unittest.TestCase):
    def test_the_double_quote_becomes_an_entity(self):
        self.assertEqual(escape_html('say "hi"'), "say &quot;hi&quot;")

    def test_angle_brackets_become_entities(self):
        self.assertEqual(escape_html("<b>"), "&lt;b&gt;")

    def test_the_ampersand_is_escaped_only_once(self):
        self.assertEqual(escape_html("a&b"), "a&amp;b")


class SubstitutedValuesAreEscaped(unittest.TestCase):
    def test_markup_in_a_value_is_shown_as_text(self):
        self.assertEqual(
            render("<p>{{msg}}</p>", {"msg": "<b>hi</b>"}),
            "<p>&lt;b&gt;hi&lt;/b&gt;</p>",
        )

    def test_a_quote_in_a_value_cannot_close_an_attribute(self):
        self.assertEqual(
            render('<a title="{{t}}">x</a>', {"t": 'he said "hi"'}),
            '<a title="he said &quot;hi&quot;">x</a>',
        )

    def test_an_ampersand_in_a_value_is_escaped(self):
        self.assertEqual(render("{{v}}", {"v": "a&b"}), "a&amp;b")

    def test_a_script_tag_in_a_value_is_defused(self):
        self.assertEqual(
            render("{{v}}", {"v": "<script>x()</script>"}),
            "&lt;script&gt;x()&lt;/script&gt;",
        )


if __name__ == "__main__":
    unittest.main()
