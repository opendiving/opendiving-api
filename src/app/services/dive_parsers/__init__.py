from ...schemas.parsed_dive import ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError
from .suunto_json import SuuntoJsonParser
from .suunto_xml import SuuntoXmlParser

# Register new dive-computer parsers here, in the order they should be tried.
_PARSERS: list[type[DiveParser]] = [
    SuuntoXmlParser,
    SuuntoJsonParser,
]


# Every registered parser by its `key`, for resolving a stored/attested `parser_key`
# back to the parser that produced it - see `services/dive_files.py`, which uses it to
# get a `content_type` without trusting one from the client.
PARSER_BY_KEY: dict[str, type[DiveParser]] = {parser.key: parser for parser in _PARSERS}


def parse_dive_file_with_parser(filename: str, content: bytes) -> tuple[type[DiveParser], ParsedDiveSchema]:
    """Parse a dive-computer export file, returning the parser that succeeded alongside
    the result.

    Callers that store the file need to know *which* parser read it, and that is not
    always the first one whose `can_parse` matched: a parser that recognizes a file and
    then finds it isn't really its format raises `UnsupportedDiveFileError` from
    `parse()`, and the next candidate gets a turn. Only the parser that actually
    returned a result may be recorded.

    Raises:
        UnsupportedDiveFileError: if no registered parser recognizes the file.
        DiveParseError: if a parser recognizes the file but fails to parse it.
    """
    for parser in _PARSERS:
        if not parser.can_parse(filename, content):
            continue
        try:
            return parser, parser.parse(content)
        except UnsupportedDiveFileError:
            continue
    raise UnsupportedDiveFileError(f"No parser available for file: {filename}")


def parse_dive_file(filename: str, content: bytes) -> ParsedDiveSchema:
    """Parse a dive-computer export file using the first registered parser that supports it.

    Raises:
        UnsupportedDiveFileError: if no registered parser recognizes the file.
        DiveParseError: if a parser recognizes the file but fails to parse it.
    """
    return parse_dive_file_with_parser(filename, content)[1]


__all__ = [
    "PARSER_BY_KEY",
    "DiveParseError",
    "DiveParser",
    "UnsupportedDiveFileError",
    "parse_dive_file",
    "parse_dive_file_with_parser",
]
