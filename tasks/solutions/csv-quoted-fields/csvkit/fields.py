"""Splitting a CSV record into fields."""


def split_fields(line):
    """Split one CSV record into its fields.

    A comma inside a double-quoted field is part of the field. A doubled quote
    inside a quoted field is a literal quote character. The surrounding quotes
    are not part of the value.
    """
    fields = []
    current = []
    in_quotes = False
    index = 0

    while index < len(line):
        character = line[index]

        if in_quotes:
            if character == '"':
                # A doubled quote is an escaped literal; a lone one ends the
                # quoted section.
                if index + 1 < len(line) and line[index + 1] == '"':
                    current.append('"')
                    index += 2
                    continue
                in_quotes = False
                index += 1
                continue
            current.append(character)
            index += 1
            continue

        if character == '"':
            in_quotes = True
            index += 1
            continue
        if character == ",":
            fields.append("".join(current))
            current = []
            index += 1
            continue

        current.append(character)
        index += 1

    # The final field has no trailing comma to close it, so it is always
    # appended here -- which is also why an empty record is one empty field.
    fields.append("".join(current))
    return fields
