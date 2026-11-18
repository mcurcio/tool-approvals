"""Reading gateway attributes out of a gateway's metadata, for the mappings that copy them into a GatewayContext.

The rule every mapping follows: an attribute whose source is missing, or is not a string, is left out of
`GatewayContext.attributes` (`present`); nothing a mapping was not told to copy is copied.
"""

from collections.abc import Mapping
from types import MappingProxyType

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


def present(found: Mapping[str, str | None]) -> Mapping[str, str]:
    """The attributes a mapping found, by name, without the ones it did not find (None): a read-only copy, ready
    for `GatewayContext.attributes`."""
    return MappingProxyType({name: value for name, value in found.items() if value is not None})
