from ...schemas.parsed_dive import ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError
from .suunto import SuuntoXmlParser

# Register new dive-computer parsers here, in the order they should be tried.
_PARSERS: list[type[DiveParser]] = [
    SuuntoXmlParser,
]


def parse_dive_file(filename: str, content: bytes) -> ParsedDiveSchema:
    """Parse a dive-computer export file using the first registered parser that supports it.

    Raises:
        UnsupportedDiveFileError: if no registered parser recognizes the file.
        DiveParseError: if a parser recognizes the file but fails to parse it.
    """
    for parser in _PARSERS:
        if parser.can_parse(filename, content):
            return parser.parse(content)
    raise UnsupportedDiveFileError(f"No parser available for file: {filename}")


__all__ = [
    "DiveParseError",
    "DiveParser",
    "UnsupportedDiveFileError",
    "parse_dive_file",
]
