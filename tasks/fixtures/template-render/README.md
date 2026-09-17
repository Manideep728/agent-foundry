# tmpl

A minimal HTML template renderer, in two parts.

- `tmpl.escaping.escape_html` turns one value into HTML-safe text.
- `tmpl.render.render` fills `{{name}}` placeholders in a template.

Rules they are meant to honour together:

- A placeholder is replaced by the matching entry from the values mapping.
- **A substituted value is always escaped**, so a value carrying markup is
  shown as text and cannot inject elements.
- The literal text of the template is left exactly as written. A template is
  trusted and normally contains real markup; escaping it would render the
  page as source.
- Escaping covers `&`, `<`, `>` and the double quote. The double quote
  matters because values are substituted into attributes such as
  `title="{{t}}"`, where an unescaped quote closes the attribute early.
- The ampersand is escaped first, so an entity this produces is not escaped
  a second time.
- A placeholder with no matching entry is a `KeyError`.
