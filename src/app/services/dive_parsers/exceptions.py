class DiveParseError(Exception):
    """Raised when a dive file was recognized by a parser but could not be parsed."""


class UnsupportedDiveFileError(Exception):
    """Raised when no registered parser can handle the given file."""
