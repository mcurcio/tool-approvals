"""Reading gateway attributes out of a gateway's metadata, for the mappings that copy them into a GatewayContext.

The rule every mapping follows: an attribute whose source is missing, or is not a string, is left out of
`GatewayContext.attributes`; nothing a mapping was not told to copy is copied.
"""

from google.protobuf import struct_pb2


def struct_string(struct: struct_pb2.Struct, path: tuple[str, ...]) -> str | None:
    """The string at `path` (one or more keys, each descending into the struct the one before names), or None
    when a step is missing, an intermediate value is not a struct, or the leaf is not a string."""
    *parents, leaf = path
    for key in parents:
        value = struct.fields.get(key)
        if value is None or value.WhichOneof("kind") != "struct_value":
            return None
        struct = value.struct_value
    value = struct.fields.get(leaf)
    if value is None or value.WhichOneof("kind") != "string_value":
        return None
    return value.string_value
