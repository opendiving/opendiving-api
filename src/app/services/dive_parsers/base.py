from abc import ABC, abstractmethod
from typing import ClassVar

from ...schemas.dive_profile import ParsedProfileSchema
from ...schemas.parsed_dive import ParsedDiveSchema


class DiveParser(ABC):
    """Base interface for dive-computer export file parsers.

    To support a new dive-computer format, implement this interface and
    register the class in `dive_parsers.__init__`.
    """

    # Stable identifier for this format, recorded on any stored export (`dive_file.
    # parser_key`) and carried in the token minted by `/dive/parse`. Stored rows are
    # queried by it when developing a new extraction ("re-run this against every Suunto
    # JSON we have"), so treat it as part of the data model: renaming one orphans every
    # row already written under the old name.
    key: ClassVar[str]
    # What a stored export of this format is served back as. Comes from here rather than
    # from the uploader's claimed `Content-Type` or a byte sniff, since a successful
    # parse is a stronger guarantee than either - and this way the value in the response
    # header is always one of a closed set the application itself declares.
    content_type: ClassVar[str]

    @classmethod
    @abstractmethod
    def can_parse(cls, filename: str, content: bytes) -> bool:
        """Cheap check (e.g. file extension) for whether this parser should attempt the file.

        Prefer a purely syntactic check (e.g. just the filename) that avoids parsing
        `content` at all, leaving that to `parse()`. This isn't always enough to narrow
        down which parser(s) are worth trying, though (e.g. a `.json` file could be any
        number of unrelated formats) - in that case, `can_parse` may need to attempt a
        cheap, defensive parse of `content` itself to check for a distinctive shape,
        as long as it never raises: unparseable/unrecognized content should be treated
        as `False`, not propagated as an exception.
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

    @classmethod
    def parse_profile(cls, content: bytes) -> ParsedProfileSchema | None:
        """Extract this dive's per-sample profile, or `None` when the file carries none.

        Separate from `parse()` and deliberately **not** abstract: a new format can ship
        header-only and grow a profile extraction later without a flag day, and the
        default here is the honest answer for a parser that hasn't got one yet.

        Never called on the `/dive/parse` path - only server-side from
        `PUT /dive/{uuid}/file`, which is the only place that has both the bytes and
        proof of where they came from. See `models/dive_profile.py`.

        Raises:
            DiveParseError: if the file's samples are malformed. A file with no samples
                at all is `None`, not an error.
        """
        return None
