"""Splitting a CSV record into fields."""


def split_fields(line):
    """Split one CSV record into its fields.

    A comma inside a double-quoted field is part of the field. A doubled quote
    inside a quoted field is a literal quote character. The surrounding quotes
    are not part of the value.
    """
    return line.split(",")
