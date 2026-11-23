"""a phone original stored unlabelled is labelled for its reader

Revision ID: 36f9496a7b33
Revises: 624702237252
Create Date: 2026-11-23 22:08:35.586397

Until 2026-11-04 the iOS app uploaded each dive's `.bin` without the tag naming the computer that
wrote it, and the import stored those under `divejson_import`, a key no reader reads. The app
downloads two computers, the Shearwater Perdix 3 and the Suunto Ocean, so for a `.bin` on a
recording naming one of those models the tag is not a guess: this writes the key and the
descriptor the app writes for that model today, and, for the Ocean, a fallback start. Nothing
else is labelled. See *A phone original stored unlabelled is labelled from its recording's model*
in DECISIONS.md.

The bytes, the digest, the recording and its profile are untouched. The worker reads the fixes
of a file marked unread, and a dive's position is its first file's by id that has one, so a
labelled file is marked unread only where its dive has no position, and no dive's position
changes but one it lacks.

`downgrade()` is a no-op: the label is what the phone writes for these computers, and a database
stepped back reads a labelled file as it reads one the phone tagged.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "36f9496a7b33"
down_revision: str | None = "624702237252"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Module-level so `tests/test_phone_original_labels.py` runs the shipped text against a live
# Postgres. The models are folded as the gates fold a device string, trimmed and case-folded,
# and the brand is not read: the recordings carry it in several spellings and none.
#
# The fallback start is the recording's own start, written only where that start is this file's:
# not on a merge's recording, whose start is the merged dive's, and only where the file's name -
# the phone's `…_YYYY-MM-DDTHHMM.bin`, in the dive's wall clock - names the recording's minute,
# which also catches a merge whose `merge` profile a re-derivation replaced. Elsewhere the file
# has none, which says nothing of its record, where a wrong one would name another record's start.
LABEL = r"""
UPDATE dive_file AS f
   SET parser_key = 'libdivecomputer',
       descriptor_vendor = label.vendor,
       descriptor_product = label.product,
       descriptor_model = label.model,
       fallback_start = CASE
           WHEN label.falls_back
            AND NOT EXISTS (
                SELECT 1 FROM dive_profile AS p WHERE p.recording_id = r.id AND p.parser_key = 'merge'
            )
            AND right(f.original_filename, 19) = to_char(
                r.start_time AT TIME ZONE 'UTC' + make_interval(mins => COALESCE(r.utc_offset_minutes, 0)),
                'YYYY-MM-DD"T"HH24MI'
            ) || '.bin'
           THEN r.start_time
       END,
       fixes_read = d.entry_latitude IS NOT NULL OR d.exit_latitude IS NOT NULL
  FROM dive_recording AS r
  JOIN (
      VALUES ('perdix 3', 'Shearwater', 'Perdix 3', 14, false),
             ('suunto ocean', 'Suunto', 'Ocean', 1, true)
  ) AS label (folded_model, vendor, product, model, falls_back)
    ON label.folded_model = lower(regexp_replace(r.device_model, '^\s+|\s+$', '', 'g')),
       dive AS d
 WHERE r.id = f.recording_id
   AND d.id = f.dive_id
   AND f.parser_key = 'divejson_import'
   AND f.content_type = 'application/octet-stream'
   AND f.original_filename LIKE '%.bin'
"""


def upgrade() -> None:
    op.execute(LABEL)


def downgrade() -> None:
    """Nothing to undo - see the module docstring."""
