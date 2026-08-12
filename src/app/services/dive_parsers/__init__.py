import logging

from ...schemas.parsed_dive import ParsedDiveSchema
from .base import DiveParser
from .exceptions import DiveParseError, UnsupportedDiveFileError
from .fit import FitParser
from .suunto_json import SuuntoJsonParser
from .suunto_xml import SuuntoXmlParser

logger = logging.getLogger(__name__)

# Register new dive-computer parsers here, in the order they should be tried.
_PARSERS: list[type[DiveParser]] = [
    SuuntoXmlParser,
    SuuntoJsonParser,
    FitParser,
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

    Anything a parser raises that isn't one of those two is converted here rather than
    left to reach the route, which knows only those two and would answer a corrupt upload
    with a 500. Each parser still guards its own failure modes and produces a better
    message than this can; the backstop exists because every one of them drives a
    third-party decoder over bytes a stranger supplied, and "the parsers are careful" is
    not the same guarantee as "the endpoint cannot 500". Logged with the parser key, since
    a file reaching here is either a bug worth seeing or a corpus entry worth having.

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
        except DiveParseError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error parsing a %s file", parser.key)
            raise DiveParseError(f"Could not read this {parser.key} file: {exc or type(exc).__name__}") from exc
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
