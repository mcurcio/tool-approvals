"""The rules between sections, checked when the file is read, before any secret is resolved.

Each check returns every problem it finds, so a refused file lists them all. A name a check needs from a section
the file leaves out is unknown: a requirement naming a level needs `assurance`, a policy naming a requirement
needs `requirements`.
"""

import importlib.util
from collections import Counter
from collections.abc import Iterator
from typing import TYPE_CHECKING, Final

from tool_approvals.config.sections import (
    GATEWAY_ADAPTERS,
    NO_APPROVAL,
    BearerCredential,
    ClientCertCredential,
    Listener,
)
from tool_approvals.kernel.methods import Method
from tool_approvals.kernel.names import AssuranceName
from tool_approvals.kernel.requirements import meets_floor

if TYPE_CHECKING:
    from tool_approvals.config.file import ConfigFile

# The lock waits one check can meet in a row, each bounded by approvals.database.lock_timeout: Claim's row lock and
# EVENTS lock, then Request's principal lock and EVENTS lock. All of them must fit in gate.check_budget, so a slow
# lock is refused by Approvals as unavailable (and logged there as a lock wait) well before the gate's budget runs
# out mid-commit.
LOCK_WAITS_PER_CHECK: Final = 4


def problems(cfg: "ConfigFile") -> list[str]:
    """Every cross-section problem in `cfg`; empty when it may be read."""
    return [
        *_assurance(cfg),
        *_surfaces(cfg),
        *_requirements(cfg),
        *_registry(cfg),
        *_listeners(cfg),
        *_gate(cfg),
        *_approvals(cfg),
        *_answerable(cfg),
        *_lock_waits(cfg),
    ]


def _repeated(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _assurance(cfg: "ConfigFile") -> Iterator[str]:
    levels = cfg.assurance or ()
    for name in _repeated([level.name for level in levels]):
        yield f"assurance: the level {name!r} is named twice"
    for acr in _repeated([acr for level in levels for acr in level.acr]):
        yield f"assurance: the acr {acr!r} is listed twice"


def _level(cfg: "ConfigFile", where: str, name: str) -> Iterator[str]:
    if cfg.scale().rank(AssuranceName(name)) is None:
        yield f"{where}: unknown assurance level {name!r}"


def _surfaces(cfg: "ConfigFile") -> Iterator[str]:
    surfaces = cfg.surfaces or ()
    for name in _repeated([surface.name for surface in surfaces]):
        yield f"surfaces: the surface {name!r} is named twice"
    for surface in surfaces:
        where = f"surfaces.{surface.name}"
        yield from _level(cfg, f"{where}.cap", surface.cap)
        if not surface.binds_digest:  # every surface is native (unit 3 adds integrated ones)
            yield f"{where}: a native surface must bind the digest (binds_digest: true)"
    if cfg.floor is not None:
        yield from _level(cfg, "floor.min_assurance", cfg.floor.min_assurance)


def _requirements(cfg: "ConfigFile") -> Iterator[str]:
    scale = cfg.scale()
    surfaces = {surface.name: surface for surface in cfg.surfaces or ()}
    for name, section in (cfg.requirements or {}).items():
        where = f"requirements.{name}"
        if name == NO_APPROVAL:
            yield f"{where}: {NO_APPROVAL!r} is reserved: a policy's `require: {NO_APPROVAL}` asks for no approval"
        yield from _level(cfg, f"{where}.min_assurance", section.min_assurance)
        requirement = section.kernel(name)
        minimum = scale.rank(requirement.min_assurance)
        # An unknown level is reported once, above, not again as below the floor.
        floor = cfg.floor.kernel() if cfg.floor is not None else None
        known_levels = floor is not None and None not in (minimum, scale.rank(floor.min_assurance))
        if floor is not None and known_levels and not meets_floor(requirement, floor, scale):
            yield f"{where}: below the floor (its minimum level or sign-in age is looser than `floor`'s)"
        if not section.approver_groups:
            yield f"{where}.approver_groups: empty, so no one can answer it"
        if not section.allowed_surfaces:
            yield f"{where}.allowed_surfaces: empty, so it can be answered nowhere"
        for surface_name in section.allowed_surfaces:
            surface = surfaces.get(surface_name)
            if surface is None:
                yield f"{where}.allowed_surfaces: unknown surface {surface_name!r}"
                continue
            cap = scale.rank(AssuranceName(surface.cap))
            if minimum is not None and cap is not None and cap < minimum:
                yield (
                    f"{where}: the surface {surface_name!r} it allows caps at {surface.cap!r}, below its minimum "
                    f"{section.min_assurance!r}"
                )


def _registry(cfg: "ConfigFile") -> Iterator[str]:
    entries = cfg.registry or ()
    bearers = [
        (entry.name, c.bearer_sha256) for entry in entries if isinstance(c := entry.credential, BearerCredential)
    ]
    certs = [
        (entry.name, c.client_cert_sha256)
        for entry in entries
        if isinstance(c := entry.credential, ClientCertCredential)
    ]
    for what, named in (("bearer hash", bearers), ("client certificate", certs)):
        for repeated in _repeated([digest.hex() for _, digest in named]):
            names = ", ".join(name for name, digest in named if digest.hex() == repeated)
            yield f"registry: one {what} on two entries ({names})"
    yield from _client_certificates(cfg)


# The grants a client-certificate credential may carry, and the listeners that serve each (question 10): the calls
# a gateway makes. Calls between this project's own services keep their bearers.
_CERTIFICATE_LISTENERS: Final = {
    Method.CHECK: (
        "gate.check_api.grpc",
        "gate.check_api.json",
        *(f"gate.{name}.listener" for name in GATEWAY_ADAPTERS),
    ),
    Method.WAIT_CALL: ("wait.listeners.mcp",),
}


def _client_certificates(cfg: "ConfigFile") -> Iterator[str]:
    """A client-certificate entry is granted only what a gateway calls, and some listener serving one of its grants
    asks for client certificates. Such a listener asks every caller for one, bearer callers too (question 9)."""
    asking = {where for where, listener in _bearer_listeners(cfg) if listener.tls and listener.tls.client_ca}
    for entry in cfg.registry or ():
        if not isinstance(entry.credential, ClientCertCredential):
            continue
        where = f"registry.{entry.name}"
        if not set(entry.grants) <= set(_CERTIFICATE_LISTENERS):
            yield f"{where}: a client-certificate credential is accepted only for check and wait.call"
            continue  # reported once: no listener serves the grants it may not carry
        serving = {listener for grant in entry.grants for listener in _CERTIFICATE_LISTENERS[grant]}
        if not serving & asking:
            yield (
                f"{where}: a client-certificate credential, but no listener serving its grants asks for client "
                "certificates (tls.client_ca)"
            )


def _bearer_listeners(cfg: "ConfigFile") -> Iterator[tuple[str, Listener]]:
    """Every listener whose callers authenticate with a registry bearer."""
    if cfg.gate is not None:
        if cfg.gate.check_api is not None:
            if cfg.gate.check_api.grpc is not None:
                yield "gate.check_api.grpc", cfg.gate.check_api.grpc
            if cfg.gate.check_api.json_ is not None:
                yield "gate.check_api.json", cfg.gate.check_api.json_
        for name, adapter in cfg.gate.adapters():
            yield f"gate.{name}.listener", adapter.listener
    if cfg.wait is not None:
        yield "wait.listeners.mcp", cfg.wait.listeners.mcp
    if cfg.approvals is not None:
        yield "approvals.listeners.requests", cfg.approvals.listeners.requests


def _listeners(cfg: "ConfigFile") -> Iterator[str]:
    for where, listener in _bearer_listeners(cfg):
        if listener.tls is None:
            yield f"{where}: carries registry bearers, so it needs tls"


def _gate(cfg: "ConfigFile") -> Iterator[str]:
    gate = cfg.gate
    if gate is None:
        return
    for name, adapter in gate.adapters():
        if not gate.check_budget < adapter.hook_timeout:
            yield f"gate.check_budget: not below gate.{name}.hook_timeout"
        if adapter.needs is not None and not _installed(adapter.needs.module):
            extra = adapter.needs.name
            yield f"gate.{name}: needs the {extra} extra, which is not installed (tool-approvals[{extra}])"
    known = set(cfg.requirements or {})
    named = [(f"gate.policy.rules[{index}].require", rule.require) for index, rule in enumerate(gate.policy.rules)]
    for where, name in [*named, ("gate.policy.default", gate.policy.default)]:
        if name != NO_APPROVAL and name not in known:
            yield f"{where}: unknown requirement {name!r}"
    yield from _rule_attributes(cfg)


def _installed(module: str) -> bool:
    """Whether `module` can be imported (its parent packages are imported to find it)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # a parent missing, or blocked in sys.modules
        return False


def _rule_attributes(cfg: "ConfigFile") -> Iterator[str]:
    """Every attribute a policy rule reads is supplied by every enabled gateway adapter, and by at least one enabled
    mapping (the Check API's caller sends its own)."""
    gate = cfg.gate
    if gate is None:
        return
    mapped = {name: set(adapter.attributes) for name, adapter in gate.adapters()}
    for index, rule in enumerate(gate.policy.rules):
        for name in rule.attributes:
            where = f"gate.policy.rules[{index}].attributes.{name}"
            for mapping, names in mapped.items():
                if name not in names:
                    yield f"{where}: not mapped by gate.{mapping}.attributes"
            if gate.check_api is None and not any(name in names for names in mapped.values()):
                yield f"{where}: no enabled mapping supplies it"


def _approvals(cfg: "ConfigFile") -> Iterator[str]:
    approvals = cfg.approvals
    if approvals is None:
        return
    # Every surface is native until unit 3 adds integrated ones, so a known name is a native surface.
    if approvals.desk.surface not in {surface.name for surface in cfg.surfaces or ()}:
        yield f"approvals.desk.surface: unknown surface {approvals.desk.surface!r}"
    # A Wait Task lives (its ttl) until the answer deadline plus the use window: retention keeps the Approval that
    # long at least, so a task is never told its wait is unknown while it may still be followed.
    lifetimes = [section.answer_deadline + section.use_window for section in (cfg.requirements or {}).values()]
    if lifetimes and approvals.retention < max(lifetimes):
        yield "approvals.retention: below the longest answer_deadline + use_window of a requirement"


def _lock_waits(cfg: "ConfigFile") -> Iterator[str]:
    """approvals.database.lock_timeout well below gate.check_budget, when the file configures both."""
    if cfg.gate is None or cfg.approvals is None:
        return
    if cfg.approvals.database.lock_timeout * LOCK_WAITS_PER_CHECK > cfg.gate.check_budget:
        yield (
            f"approvals.database.lock_timeout: more than 1/{LOCK_WAITS_PER_CHECK} of gate.check_budget "
            f"({LOCK_WAITS_PER_CHECK} lock waits in a row must fit in one check)"
        )


def _answerable(cfg: "ConfigFile") -> Iterator[str]:
    """Every requirement answerable at this Approvals: its groups read from the claim the desk's IdP sends, and
    the desk's surface among those it allows (the desk is the one surface that answers until milestone 3)."""
    approvals = cfg.approvals
    if approvals is None:
        return
    claim, desk = approvals.idp.groups_claim, approvals.desk.surface
    for name, section in (cfg.requirements or {}).items():
        where = f"requirements.{name}"
        for index, group in enumerate(section.approver_groups):
            if group.claim != claim:
                yield (
                    f"{where}.approver_groups[{index}].claim: {group.claim!r}, but approvals.idp.groups_claim is "
                    f"{claim!r}, so no sign-in can carry it"
                )
        known_desk = desk in {surface.name for surface in cfg.surfaces or ()}  # an unknown one is reported once
        if known_desk and section.allowed_surfaces and desk not in section.allowed_surfaces:
            yield f"{where}.allowed_surfaces: does not allow approvals.desk.surface {desk!r}, so no one can answer it"
