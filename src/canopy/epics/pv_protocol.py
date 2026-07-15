"""PV-name protocol prefix parsing: ``ca://`` (Channel Access) / ``pva://``
(PV Access). Unprefixed names default to CA.

Prefix matching is case-insensitive: ``PVA://X``, ``Pva://X`` and ``pva://X``
are all recognized as PV Access. This matters because an unrecognized prefix
would otherwise be treated as a literal CA channel name (colons and slashes
included) and handed straight to the EPICS C library, which was the cause of
the ``PVA://`` crash.

``validate_pv_name`` is the guard applied at every boundary where a
client-supplied name enters the system, so an empty, over-long, or otherwise
malformed name is rejected with a clean 400 rather than reaching aioca/p4p.
"""

from canopy.core.errors import ErrorCode, BadRequest

PVA_PREFIX = "pva://"
CA_PREFIX = "ca://"

# EPICS channel names are short in practice (well under 60 chars). This cap is
# deliberately generous; its only job is to reject pathological inputs before
# they reach the C library.
MAX_PV_NAME_LEN = 255


class InvalidPVName(BadRequest):
    """A client supplied a PV name that is empty or malformed.

    A ``BadRequest`` (HTTP 400) rather than a generic 500: the input is the
    problem, and rejecting it here keeps empty/garbage names out of the EPICS
    C library, where they can fault the whole process.
    """

    error_code = ErrorCode.INVALID_PV_NAME


def _starts_with_prefix(pv_name: str, prefix: str) -> bool:
    """Case-insensitive prefix check."""
    return pv_name[: len(prefix)].lower() == prefix


def parse_pv_name(pv_name: str) -> tuple[str, str]:
    """Return ``(protocol, stripped_name)`` where protocol is ``"ca"`` or ``"pva"``.

    The protocol prefix is matched case-insensitively; the returned name has the
    prefix removed but is otherwise unchanged. Callers that accept client input
    should run it through :func:`validate_pv_name` first.
    """
    if _starts_with_prefix(pv_name, PVA_PREFIX):
        return "pva", pv_name[len(PVA_PREFIX) :]
    if _starts_with_prefix(pv_name, CA_PREFIX):
        return "ca", pv_name[len(CA_PREFIX) :]
    return "ca", pv_name


def is_pva(pv_name: str) -> bool:
    return _starts_with_prefix(pv_name, PVA_PREFIX)


def is_ca(pv_name: str) -> bool:
    return _starts_with_prefix(pv_name, CA_PREFIX)


def has_protocol_prefix(pv_name: str) -> bool:
    return is_pva(pv_name) or is_ca(pv_name)


def is_unprefixed(pv_name: str) -> bool:
    return not has_protocol_prefix(pv_name)


def strip_protocol_prefix(pv_name: str) -> str:
    if _starts_with_prefix(pv_name, PVA_PREFIX):
        return pv_name[len(PVA_PREFIX) :]
    if _starts_with_prefix(pv_name, CA_PREFIX):
        return pv_name[len(CA_PREFIX) :]
    return pv_name


def _has_control_chars(text: str) -> bool:
    """True if `text` contains a NUL, other C0 control char, or DEL."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _has_whitespace(text: str) -> bool:
    """True if `text` contains any whitespace (space, tab, newline, ...).

    EPICS channel names never contain whitespace. Catching it here rejects the
    common spreadsheet copy-paste case (``"  PV:NAME  "`` or an interior space)
    with a clean 400 instead of letting a space-padded name reach aioca/p4p,
    where it fails as a silent connection timeout rather than a validation error.
    """
    return any(ch.isspace() for ch in text)


def validate_pv_name(pv_name: str) -> str:
    """Return `pv_name` unchanged if it is a well-formed PV name, else raise.

    Rejects, with :class:`InvalidPVName` (HTTP 400):

    - non-strings and the empty / whitespace-only string;
    - names longer than :data:`MAX_PV_NAME_LEN`;
    - names containing any whitespace (leading, trailing, or interior) — a
      space-padded name would otherwise reach aioca/p4p and fail as a silent
      connection timeout instead of a clean 400;
    - names containing NUL / control characters (which corrupt C string
      handling in the EPICS libraries);
    - a bare protocol prefix with no channel name after it (``"pva://"``);
    - a name that still contains ``"://"`` after its prefix is stripped
      (e.g. ``"pva://ca://x"`` or a mistyped double prefix).

    This is the single guard every client-facing boundary uses so that a
    malformed name can never reach aioca / p4p.
    """
    if not isinstance(pv_name, str):
        raise InvalidPVName(f"PV name must be a string, got {type(pv_name).__name__}")
    if not pv_name.strip():
        raise InvalidPVName("PV name must not be empty")
    if len(pv_name) > MAX_PV_NAME_LEN:
        raise InvalidPVName(f"PV name exceeds {MAX_PV_NAME_LEN} characters")
    if _has_whitespace(pv_name):
        raise InvalidPVName("PV name contains whitespace")
    if _has_control_chars(pv_name):
        raise InvalidPVName("PV name contains control characters")
    stripped = strip_protocol_prefix(pv_name)
    if not stripped.strip():
        raise InvalidPVName("PV name is empty after its protocol prefix")
    if "://" in stripped:
        raise InvalidPVName("PV name contains an unexpected protocol prefix")
    return pv_name
