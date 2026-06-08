"""PV-name protocol prefix parsing: ``ca://`` (Channel Access) / ``pva://``
(PV Access). Unprefixed names default to CA.
"""

PVA_PREFIX = "pva://"
CA_PREFIX = "ca://"


def parse_pv_name(pv_name: str) -> tuple[str, str]:
    """Return ``(protocol, stripped_name)`` where protocol is ``"ca"`` or ``"pva"``."""
    if pv_name.startswith(PVA_PREFIX):
        return "pva", pv_name[len(PVA_PREFIX) :]
    if pv_name.startswith(CA_PREFIX):
        return "ca", pv_name[len(CA_PREFIX) :]
    return "ca", pv_name


def is_pva(pv_name: str) -> bool:
    return pv_name.startswith(PVA_PREFIX)


def is_ca(pv_name: str) -> bool:
    return pv_name.startswith(CA_PREFIX)


def has_protocol_prefix(pv_name: str) -> bool:
    return is_pva(pv_name) or is_ca(pv_name)


def is_unprefixed(pv_name: str) -> bool:
    return not has_protocol_prefix(pv_name)


def strip_protocol_prefix(pv_name: str) -> str:
    if pv_name.startswith(PVA_PREFIX):
        return pv_name[len(PVA_PREFIX) :]
    if pv_name.startswith(CA_PREFIX):
        return pv_name[len(CA_PREFIX) :]
    return pv_name
