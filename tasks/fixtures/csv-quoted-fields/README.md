# csvkit

A minimal CSV field splitter.

`csvkit.fields.split_fields` takes one CSV record and returns its fields.

Quoting rules it is meant to honour:

- A comma inside a double-quoted field is part of the field, not a separator.
- A doubled quote inside a quoted field is a literal quote character.
- The surrounding quotes are not part of the field's value.
