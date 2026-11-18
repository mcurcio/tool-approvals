"""struct_string: a string leaf at a key path into a protobuf Struct, or None; present: the attributes a mapping
found, without the missing ones, read-only."""

import pytest
from google.protobuf import struct_pb2

from tool_approvals.gate.mappings.attributes import present, struct_string


def _struct() -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    struct.update({"tenant": "blue", "empty": "", "n": 3, "flag": True, "jwt": {"claims": {"tenant": "green"}}})
    return struct


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("tenant",), "blue"),
        (("empty",), ""),  # an empty string is still a string
        (("jwt", "claims", "tenant"), "green"),  # nested keys
        (("absent",), None),
        (("jwt", "absent", "tenant"), None),  # a missing step
        (("tenant", "more"), None),  # a step through a string
        (("n",), None),  # a number
        (("flag",), None),  # a bool
        (("jwt",), None),  # a struct
        (("jwt", "claims"), None),
    ],
)
def test_only_a_string_leaf_is_read(path: tuple[str, ...], value: str | None) -> None:
    assert struct_string(_struct(), path) == value


def test_present_leaves_out_what_was_not_found_and_is_read_only() -> None:
    found = {"tenant": "blue", "zone": None, "empty": ""}
    kept = present(found)
    assert dict(kept) == {"tenant": "blue", "empty": ""}
    with pytest.raises(TypeError):
        kept["zone"] = "eu"  # type: ignore[index]
    found["tenant"] = "green"
    assert kept["tenant"] == "blue"  # a copy, not a view of what the mapping still holds
