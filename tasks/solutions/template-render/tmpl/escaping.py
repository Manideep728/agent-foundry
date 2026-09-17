"""Turning a value into HTML-safe text."""


def escape_html(value):
    """Escape the characters that are unsafe in HTML text and attributes.

    Replaces the ampersand, the angle brackets and the double quote with their
    entities. The double quote is included because values are substituted into
    attributes such as title="{{t}}", where an unescaped quote closes the
    attribute early. The ampersand is replaced first, so an entity produced
    here is not escaped a second time.
    """
    text = str(value)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text
