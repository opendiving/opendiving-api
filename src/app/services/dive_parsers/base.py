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
        """Cheap, syntactic check (e.g. file extension) for whether this parser should attempt the file.

        This must not parse `content`, since `parse()` will do that. It only narrows down which
        parser(s) are worth trying.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def parse(cls, content: bytes) -> ParsedDiveSchema:
        """Parse file content into a ParsedDiveSchema.

        Raises:
            UnsupportedDiveFileError: if the content turns out not to be in a format this parser handles
                (e.g. well-formed XML with an unrecognized root element).
            DiveParseError: if the content is malformed or cannot be parsed.
        """
        raise NotImplementedError
