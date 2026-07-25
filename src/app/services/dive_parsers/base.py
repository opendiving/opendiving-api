from abc import ABC, abstractmethod

from ...schemas.parsed_dive import ParsedDiveSchema


class DiveParser(ABC):
    """Base interface for dive-computer export file parsers.

    To support a new dive-computer format, implement this interface and
    register the class in `dive_parsers.__init__`.
    """

    @classmethod
    @abstractmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        """Return True if this parser can handle the given file."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        """Parse file content into a ParsedDiveSchema.

        Raises:
            DiveParseError: if the content is malformed or cannot be parsed.
        """
        raise NotImplementedError
