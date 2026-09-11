"""Every foreign key column leads an index of its own.

Postgres indexes the *referenced* side of a foreign key for you - it has to, the target is a
primary key or a unique constraint - and indexes the referencing side never. So a child table
whose `parent_id` carries no index is scanned in full every time something touches the parent
row: `ON DELETE CASCADE` has to find the rows to delete, a plain `DELETE` has to prove there
are none, and the app's own "everything belonging to this parent" reads go the same way. Every
foreign key into `user.id` is `CASCADE` (see `test_user_cascade.py`), so the account purge is
precisely that scan, once per table, per account.

**Leading**, not merely present. A composite index serves a lookup on its first column and not
on its later ones, so `(dive_id, position)` covers `dive_id` and `(user_id, sha256)` covers
`user_id`, while an index that mentions the foreign key second covers nothing this rule is
about. Uniqueness is irrelevant to the question - a unique index is an index - which is why
several columns here are covered only by a `ux_` one, and why a primary key or a
`UniqueConstraint` counts too: Postgres implements both with a real index, and that index
serves its leading column exactly like a declared one.

**And usable**, which rules out a partial index however well it leads. The lookup a foreign key
provokes carries no predicate for one to be implied by, so Postgres scans the table rather than
using it - measured, not reasoned about, in `DECISIONS.md` under *"The indexes are the part that
needed care, not the deletes"*, which is also where the two plain indexes on
`gear_service_record` come from. That distinction is the whole reason this test asserts a usable
index rather than any index: without it, deleting those two would leave the partial composites
standing, this test green, and the seq scan back.

This passes today and is expected to keep passing. Its job is the *next* model - the one whose
`ForeignKey(...)` arrives without `index=True` beside it and without a composite that happens
to start there. Nothing else in the suite would notice: the schema is valid, the migration
autogenerates cleanly, `alembic check` is satisfied, and the only symptom is a sequential scan
nobody is watching for on a table that was small when it was written.

No database. This is the models' own account of the rule, in the shape of
`TestEveryForeignKeyIntoUserCascades` - and it is the whole of it, because unlike the cascade
rules there is no second fact for Postgres to settle separately: an index reaches the database
only by being declared here or in a revision, and CI's `alembic check` is what pins those two
to each other.
"""

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table, UniqueConstraint, func

from src.app.core.db.database import Base
from tests.helpers.model_metadata import declared_models


def _leading_column_name(index: Index) -> str | None:
    """The column an index sorts on first, or `None` when that is not a column at all.

    `Index("ix", col.desc())` wraps the column in a `UnaryExpression` and `col.desc().nullslast()`
    - the shape `course.py` and `certification.py` use - wraps it twice, so the unwrapping runs
    until it stops finding a wrapper rather than peeling one layer and hoping.

    A functional index (`func.lower(name)`) has no leading column in the sense that matters:
    `lower(label)` does not serve a lookup on `label`. The `isinstance` check is what separates
    the two, and it earns its place - a `Function` carries a `.name` of its own (`"lower"`), so
    reading `.name` off whatever turns up would report a column called `lower` and cover nothing.
    """
    expressions = list(index.expressions)
    if not expressions:
        return None
    leading = expressions[0]
    while (inner := getattr(leading, "element", None)) is not None:
        leading = inner
    return leading.name if isinstance(leading, Column) else None


def _is_partial(index: Index) -> bool:
    """Whether the index carries a `WHERE` predicate, which disqualifies it here.

    A referential-integrity lookup carries no predicate of its own - it is
    `WHERE parent_id = $1` and nothing else - so Postgres cannot prove a partial index covers
    the rows it needs and scans the table instead. Measured rather than assumed, and the
    measurement is in `DECISIONS.md` under *"The indexes are the part that needed care, not the
    deletes"*: two partial indexes leading with the column being looked up, right table, and a
    `Seq Scan` all the same. It is why `gear_service_record` carries a plain index beside each of
    its partial ones, and why `ux_gear_service_schedule_item_kind_label` may never gain a
    predicate.
    """
    return index.dialect_kwargs.get("postgresql_where") is not None


def _columns_an_index_leads_with(table: Table) -> set[str]:
    """Every column some index on this table can be looked up by on its own."""
    leading = {
        name for index in table.indexes if not _is_partial(index) and (name := _leading_column_name(index)) is not None
    }

    # A primary key and a unique constraint are each backed by an index Postgres creates and
    # `Table.indexes` does not list, so asking only that collection would fail a foreign key
    # that is genuinely covered - an association table keyed `(parent_id, child_id)`, say.
    constraints = [table.primary_key, *(c for c in table.constraints if isinstance(c, UniqueConstraint))]
    for constraint in constraints:
        columns = list(constraint.columns)
        if columns:
            leading.add(columns[0].name)

    return leading


def foreign_key_columns_without_a_leading_index(metadata: MetaData) -> list[str]:
    """`table.column` for every foreign key column no index leads with."""
    return sorted(
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if column.foreign_keys and column.name not in _columns_an_index_leads_with(table)
    )


class TestEveryForeignKeyColumnLeadsAnIndex:
    def test_no_foreign_key_column_is_left_without_one(self) -> None:
        # Walked from disk rather than imported by hand: a model wired into its own `crud_*`
        # module reaches `Base.metadata` through that import alone, and a list of imports here
        # would be the thing that fails to mention it. See `helpers/model_metadata.py`.
        declared_models()

        assert foreign_key_columns_without_a_leading_index(Base.metadata) == []


class TestWhatCountsAsLeadingAnIndex:
    """The sweep above passes against a schema that already obeys the rule, so on its own it
    cannot tell "every foreign key is covered" from "the sweep finds nothing, ever". These are
    the cases it is supposed to separate, against synthetic tables it can fail on.
    """

    @staticmethod
    def _metadata() -> MetaData:
        """A parent and a child whose `parent_id` nothing covers yet."""
        metadata = MetaData()
        Table("parent", metadata, Column("id", Integer, primary_key=True))
        Table(
            "child",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer, ForeignKey("parent.id")),
            Column("sort_key", Integer),
            Column("label", String(16)),
        )
        return metadata

    def test_an_uncovered_foreign_key_is_reported(self) -> None:
        assert foreign_key_columns_without_a_leading_index(self._metadata()) == ["child.parent_id"]

    def test_a_single_column_index_covers_it(self) -> None:
        metadata = self._metadata()
        Index("ix_child_parent_id", metadata.tables["child"].c.parent_id)

        assert foreign_key_columns_without_a_leading_index(metadata) == []

    def test_a_composite_index_covers_it_when_it_comes_first(self) -> None:
        """`ix_trip_location_trip_id_position` and `ix_dive_recording_user_id_start_time` are
        the whole of their columns' coverage in the real schema."""
        child = self._metadata().tables["child"]
        Index("ix_child_parent_id_sort_key", child.c.parent_id, child.c.sort_key)

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_descending_second_column_does_not_hide_the_first(self) -> None:
        """The shape `ix_dive_user_id_start_time` is declared in. Nothing is unwrapped here -
        the leading expression is a plain column - which is exactly why the two cases below
        exist as well."""
        child = self._metadata().tables["child"]
        Index("ix_child_parent_id_sort_key_desc", child.c.parent_id, child.c.sort_key.desc())

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_descending_leading_column_still_counts(self) -> None:
        """`.desc()` wraps the column in a `UnaryExpression`, and an index that sorts a foreign
        key descending indexes it just as well as one that sorts it up."""
        child = self._metadata().tables["child"]
        Index("ix_child_parent_id_desc", child.c.parent_id.desc())

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_leading_column_wrapped_twice_still_counts(self) -> None:
        """`.desc().nullslast()` is two `UnaryExpression`s deep - the shape `course.py` and
        `certification.py` use for their sort columns. A single-level unwrap reports no leading
        column here and fails a table that is fine."""
        child = self._metadata().tables["child"]
        Index("ix_child_parent_id_desc_nullslast", child.c.parent_id.desc().nullslast())

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_composite_index_does_not_cover_a_column_it_mentions_second(self) -> None:
        """The case worth having a test for: the index exists, the column is in it, and a
        lookup by `parent_id` alone still scans the table."""
        child = self._metadata().tables["child"]
        Index("ix_child_sort_key_parent_id", child.c.sort_key, child.c.parent_id)

        assert foreign_key_columns_without_a_leading_index(child.metadata) == ["child.parent_id"]

    def test_a_unique_index_counts(self) -> None:
        """`ux_dive_file_user_id_sha256` covers `dive_file.user_id` and nothing else does."""
        child = self._metadata().tables["child"]
        Index("ux_child_parent_id_label", child.c.parent_id, child.c.label, unique=True)

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_unique_constraint_counts_too(self) -> None:
        """Declared as a constraint rather than an `Index`, so it never reaches
        `Table.indexes` - and Postgres backs it with an index all the same."""
        child = self._metadata().tables["child"]
        child.append_constraint(UniqueConstraint("parent_id", "label", name="uq_child_parent_id_label"))

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_unique_constraint_that_mentions_it_second_does_not(self) -> None:
        child = self._metadata().tables["child"]
        child.append_constraint(UniqueConstraint("label", "parent_id", name="uq_child_label_parent_id"))

        assert foreign_key_columns_without_a_leading_index(child.metadata) == ["child.parent_id"]

    def test_a_composite_primary_key_counts_when_it_starts_there(self) -> None:
        """The association-table shape: no surrogate `id`, no declared index, covered anyway."""
        metadata = MetaData()
        Table("parent", metadata, Column("id", Integer, primary_key=True))
        Table("other", metadata, Column("id", Integer, primary_key=True))
        Table(
            "link",
            metadata,
            Column("parent_id", Integer, ForeignKey("parent.id"), primary_key=True),
            Column("other_id", Integer, ForeignKey("other.id"), primary_key=True, index=True),
        )

        assert foreign_key_columns_without_a_leading_index(metadata) == []

    def test_a_partial_index_covers_nothing_however_well_it_leads(self) -> None:
        """`gear_service_record`'s shape before the two plain indexes were added: an index on
        the right table, leading with the right column, that the lookup cannot use. Counting it
        is the one way this whole file could pass while the scan it exists to prevent runs."""
        child = self._metadata().tables["child"]
        Index(
            "ix_child_parent_id_sort_key_partial",
            child.c.parent_id,
            child.c.sort_key,
            postgresql_where=child.c.sort_key.is_(None),
        )

        assert foreign_key_columns_without_a_leading_index(child.metadata) == ["child.parent_id"]

    def test_a_plain_index_beside_a_partial_one_covers_it(self) -> None:
        """`gear_service_record`'s shape today, and the reason those two indexes are not the
        redundant pair they look like."""
        child = self._metadata().tables["child"]
        Index(
            "ix_child_parent_id_sort_key_partial",
            child.c.parent_id,
            child.c.sort_key,
            postgresql_where=child.c.sort_key.is_(None),
        )
        Index("ix_child_parent_id", child.c.parent_id)

        assert foreign_key_columns_without_a_leading_index(child.metadata) == []

    def test_a_functional_index_covers_nothing(self) -> None:
        """`lower(label)` answers a lookup on `lower(label)` and nothing else.

        Asserted on `_leading_column_name` rather than only on the sweep, because the sweep
        cannot tell this case from an uncovered one: drop the `isinstance` guard and the
        expression's own `.name` - `"lower"` - joins the leading set, where it covers a column
        called `lower` that no table here has, and the sweep still reports `child.parent_id`.
        The two assertions below fail differently, which is the point: the first on a schema
        that lost its index, the second on a guard that stopped discriminating.
        """
        child = self._metadata().tables["child"]
        index = Index("ix_child_label_lower", func.lower(child.c.label))

        assert foreign_key_columns_without_a_leading_index(child.metadata) == ["child.parent_id"]
        assert _leading_column_name(index) is None
