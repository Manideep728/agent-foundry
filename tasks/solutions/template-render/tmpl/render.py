"""Filling placeholders in a template."""

import re

from tmpl.escaping import escape_html

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(template, values):
    """Fill {{name}} placeholders in template from the values mapping.

    Each placeholder is replaced by the matching entry, escaped with
    escape_html so that a value carrying markup is shown as text and cannot
    inject elements. The literal text of the template is left exactly as
    written, markup included: a template is trusted, and escaping it would
    render the page as source. A placeholder with no matching entry raises
    KeyError.
    """

    def replace(match):
        return escape_html(values[match.group(1)])

    return _PLACEHOLDER.sub(replace, template)
