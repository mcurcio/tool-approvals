"""The grant each gate RPC needs, for the composition root's GrpcAuth: every gate RPC is a check."""

from collections.abc import Mapping

from google.protobuf.descriptor import FileDescriptor

from tool_approvals.kernel.methods import Method


def check_grants(file: FileDescriptor, service: str) -> Mapping[str, Method]:
    """Every method path of `service` in `file`, each needing the CHECK grant."""
    full_name = file.services_by_name[service].full_name
    return {f"/{full_name}/{method.name}": Method.CHECK for method in file.services_by_name[service].methods}
