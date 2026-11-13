"""`services/logbook_import/changes.py`: the comparison alone, on hand-written dives."""

from typing import Any

from src.app.services.logbook_import.changes import dive_changes, same_content, value_at


def _dive(**members: Any) -> dict[str, Any]:
    return {"uuid": "a", "max_depth": 30.0, "mixtures": [], "recordings": [], **members}


def _recording(**members: Any) -> dict[str, Any]:
    return {"uuid": "r", "ordinal": 0, "device": {"name": None, "firmware": "1.0"}, "files": [], **members}


class TestWhatDiffers:
    def test_two_equal_dives_have_no_changes(self) -> None:
        assert dive_changes(_dive(), _dive()) == []

    def test_a_scalar_is_one_entry(self) -> None:
        assert dive_changes(_dive(), _dive(max_depth=31.5)) == [{"member": "max_depth", "before": 30.0, "after": 31.5}]

    def test_a_scalar_inside_an_element_is_addressed_by_its_index(self) -> None:
        before = _dive(recordings=[_recording()])
        after = _dive(recordings=[_recording(device={"name": "Porvoo", "firmware": "1.0"})])

        assert dive_changes(before, after) == [
            {"member": "recordings/0/device/name", "before": None, "after": "Porvoo"}
        ]

    def test_an_element_added_is_one_entry_naming_the_element(self) -> None:
        added = _recording(uuid="s", ordinal=1)
        before = _dive(recordings=[_recording()])
        after = _dive(recordings=[_recording(), added])

        assert dive_changes(before, after) == [{"member": "recordings/1", "before": None, "after": added}]

    def test_a_recording_added_at_the_front_is_one_entry_and_the_one_it_moved_none(self) -> None:
        added = _recording(uuid="s", device={"name": "Perdix", "firmware": "2.0"})
        before = _dive(recordings=[_recording()])
        after = _dive(recordings=[added, _recording(ordinal=1)])

        assert dive_changes(before, after) == [{"member": "recordings/0", "before": None, "after": added}]

    def test_a_recording_that_moved_still_reports_what_changed_on_it_where_it_now_stands(self) -> None:
        added = _recording(uuid="s")
        before = _dive(recordings=[_recording()])
        after = _dive(recordings=[added, _recording(ordinal=1, device={"name": "Porvoo", "firmware": "1.0"})])

        assert dive_changes(before, after) == [
            {"member": "recordings/0", "before": None, "after": added},
            {"member": "recordings/1/device/name", "before": None, "after": "Porvoo"},
        ]

    def test_an_element_removed_is_one_entry_with_nothing_after(self) -> None:
        gone = {"oxygen": 0.32}

        assert dive_changes(_dive(mixtures=[gone]), _dive()) == [
            {"member": "mixtures/0", "before": gone, "after": None}
        ]

    def test_a_member_appearing_where_null_stood_is_one_entry(self) -> None:
        profile = {"max_depth": 30.0, "uuid": "p"}
        before = _dive(recordings=[_recording(profile=None)])
        after = _dive(recordings=[_recording(profile=profile)])

        assert dive_changes(before, after) == [{"member": "recordings/0/profile", "before": None, "after": profile}]

    def test_a_profile_replaced_is_the_members_that_changed(self) -> None:
        before = _dive(recordings=[_recording(profile={"uuid": "p", "samples": 10, "max_depth": 30.0})])
        after = _dive(recordings=[_recording(profile={"uuid": "q", "samples": 20, "max_depth": 30.0})])

        assert dive_changes(before, after) == [{"member": "recordings/0/profile/samples", "before": 10, "after": 20}]


class TestWhatTheWriteMints:
    def test_identity_and_bookkeeping_are_no_change_at_any_depth(self) -> None:
        before = _dive(
            uuid="a",
            user_uuid="u",
            created_at="1",
            updated_at="1",
            mixtures=[{"id": 1, "oxygen": 0.21}],
            recordings=[
                _recording(uuid="r", updated_at="1", files=[{"uuid": "f", "attach_order": 0, "updated_at": "1"}])
            ],
        )
        after = _dive(
            uuid="b",
            user_uuid="v",
            created_at="2",
            updated_at="2",
            mixtures=[{"id": 2, "oxygen": 0.21}],
            recordings=[
                _recording(uuid="s", updated_at="2", files=[{"uuid": "g", "attach_order": 1, "updated_at": "2"}])
            ],
        )

        assert dive_changes(before, after) == []

    def test_a_link_to_a_gear_item_is_compared_whole(self) -> None:
        """Which record it names is the content, so its uuid counts where a row's own does not."""
        one, other = {"uuid": "g", "name": "Twinset"}, {"uuid": "h", "name": "Twinset"}
        before = _dive(mixtures=[{"id": 1, "gear_item": None}, {"id": 2, "gear_item": one}])
        after = _dive(mixtures=[{"id": 1, "gear_item": one}, {"id": 2, "gear_item": other}])

        assert dive_changes(before, after) == [
            {"member": "mixtures/0/gear_item", "before": None, "after": one},
            {"member": "mixtures/1/gear_item", "before": one, "after": other},
        ]


class TestReadingAMember:
    def test_a_path_is_walked_through_lists_and_objects(self) -> None:
        dive = _dive(recordings=[_recording(device={"name": "Porvoo"})])

        assert value_at(dive, "recordings/0/device/name") == "Porvoo"

    def test_a_path_past_what_the_read_holds_is_none(self) -> None:
        assert value_at(_dive(), "recordings/3/device/name") is None
        assert value_at(_dive(), "max_depth/0") is None

    def test_two_copies_of_an_element_are_the_same_whatever_was_minted(self) -> None:
        assert same_content({"uuid": "a", "name": "x"}, {"uuid": "b", "name": "x"})
        assert not same_content({"uuid": "a", "name": "x"}, {"uuid": "a", "name": "y"})
