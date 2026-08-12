class DiveParseError(Exception):
    """Raised when a dive file was recognized by a parser but could not be parsed."""


class UnsupportedDiveFileError(Exception):
    """Raised when no registered parser can handle the given file."""


# What turning a decoded file into a dive may raise when the file is well-formed for its
# container but holds nonsense. Every parser catches this same tuple and re-raises a
# `DiveParseError`, so the diver gets the parser's own message rather than the registry
# backstop's generic one.
#
# Shared rather than repeated per parser because it had already drifted: the FIT parser
# gained `ArithmeticError` after a corrupt float32 reading arrived as NaN and
# `Decimal.quantize` refused it, while the Suunto JSON parser - which runs the same
# `Decimal` arithmetic on values `json.loads` will happily hand back as `inf`, since it
# accepts bare `Infinity` and overflows large exponents - kept the narrower tuple. A
# cylinder pressure of `Infinity` took down an otherwise fine import, header and all.
#
# `ArithmeticError` covers `decimal.InvalidOperation` (NaN through `quantize`) and
# `OverflowError` (`timedelta(seconds=1e300)`). `AssertionError` is here because a
# third-party decoder walking hostile bytes asserts rather than raising something typed.
EXTRACTION_ERRORS = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    ArithmeticError,
    AssertionError,
)
