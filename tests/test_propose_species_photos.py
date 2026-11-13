"""Tests for `scripts/propose_species_photos.py`, the hand-run rater that proposes species photos
through the API.

The instance, Wikimedia and the model are all faked: an `httpx.MockTransport` answers for the
first two and a stand-in client for the third, so the run needs no key and no network.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "propose_species_photos.py"


def _load_script() -> Any:
    """`scripts/` is not a package and is not on the path, so it is loaded by location rather
    than imported."""
    spec = importlib.util.spec_from_file_location("propose_species_photos", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rater = _load_script()

_KEY = "odk_secret_value"
_JPEG = b"\xff\xd8\xff fake jpeg"


def _species(uuid: str, **overrides: Any) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "scientific_name": f"Species {uuid}",
        "common_name": f"common {uuid}",
        "wikidata_qid": f"Q{uuid}",
        "photo_file": "Stored.jpg",
        "accepts_photo_proposals": True,
        **overrides,
    }


class FakeInstance:
    """The API's routes the script reads and writes, over a mock transport."""

    def __init__(
        self,
        species: list[dict[str, Any]],
        *,
        proposals: list[dict[str, Any]] | None = None,
        propose_status: dict[str, int] | None = None,
    ) -> None:
        self.species = {entry["uuid"]: entry for entry in species}
        self.order = [entry["uuid"] for entry in species]
        self.proposals = proposals or []
        self.propose_status = propose_status or {}
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.species_reads: list[str] = []
        self.authorizations: set[str] = set()

    def _page(self, rows: list[dict[str, Any]], request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        page, size = int(query["page"][0]), int(query["items_per_page"][0])
        chunk = rows[(page - 1) * size : page * size]
        return httpx.Response(200, json={"data": chunk, "has_more": page * size < len(rows)})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.authorizations.add(request.headers.get("authorization", ""))
        path = request.url.path.removeprefix("/api/v1")
        if path == "/user/species":
            rows = [{"uuid": uuid, "scientific_name": self.species[uuid]["scientific_name"]} for uuid in self.order]
            return self._page(rows, request)
        if path == "/species-photo-proposals":
            return self._page(self.proposals, request)
        if request.method == "POST" and path.endswith("/photo-proposals"):
            uuid = path.split("/")[2]
            body = json.loads(request.content)
            self.posted.append((uuid, body))
            status = self.propose_status.get(uuid, 201)
            return httpx.Response(status, json={"detail": "refused"} if status != 201 else {"uuid": "p"})
        if path.startswith("/species/"):
            uuid = path.split("/")[2]
            self.species_reads.append(uuid)
            return httpx.Response(200, json=self.species[uuid])
        return httpx.Response(404, json={"detail": "Not found"})

    def client(self) -> Any:
        return rater.Instance(
            httpx.Client(
                base_url="http://instance.test/api/v1",
                headers={"Authorization": f"Bearer {_KEY}"},
                transport=httpx.MockTransport(self.handle),
            )
        )


class FakeWikimedia:
    """Wikidata, Commons and the thumbnail host, each item with its images and category."""

    def __init__(self, items: dict[str, dict[str, Any]]) -> None:
        self.items = items
        self.requests: list[httpx.Request] = []
        self.broken: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = {key: values[0] for key, values in parse_qs(urlsplit(str(request.url)).query).items()}
        host = request.url.host
        if host in self.broken:
            return httpx.Response(503)
        if host == "www.wikidata.org":
            qid = params["ids"]
            item = self.items.get(qid, {})
            claims: dict[str, Any] = {
                "P18": [
                    {"rank": "normal", "mainsnak": {"datavalue": {"value": title}}} for title in item.get("images", [])
                ]
            }
            if "category" in item:
                claims["P373"] = [{"rank": "normal", "mainsnak": {"datavalue": {"value": item["category"]}}}]
            return httpx.Response(200, json={"entities": {qid: {"claims": claims}}})
        if host == "commons.wikimedia.org" and params.get("list") == "categorymembers":
            category = params["cmtitle"].removeprefix("Category:")
            files: list[str] = next(
                (item.get("files", []) for item in self.items.values() if item.get("category") == category), []
            )
            return httpx.Response(200, json={"query": {"categorymembers": [{"title": f"File:{f}"} for f in files]}})
        if host == "commons.wikimedia.org" and params.get("prop") == "imageinfo":
            assert params["iiurlwidth"] == "250"
            pages = [
                {
                    "title": title,
                    "imageinfo": [{"width": 4000, "height": 3000, "thumburl": f"https://upload.wikimedia.org/{title}"}],
                }
                for title in params["titles"].split("|")
            ]
            return httpx.Response(200, json={"query": {"pages": pages}})
        if host == "upload.wikimedia.org":
            return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    def client(self) -> Any:
        return rater.Wikimedia(
            httpx.Client(
                headers={"User-Agent": rater.wikimedia_user_agent("ops@example.org")},
                transport=httpx.MockTransport(self.handle),
            )
        )


def _message(answer: Any, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class FakeModel:
    """A stand-in for the SDK client: records each request and answers from a queue."""

    def __init__(self, *answers: SimpleNamespace) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        return self.answers.pop(0)


def _run(instance: FakeInstance, wikimedia: FakeWikimedia, model: FakeModel, **options: Any) -> tuple[Any, list[str]]:
    printed: list[str] = []
    tally = rater.run(
        instance.client(),
        wikimedia.client(),
        lambda species, pool: rater.judge(model, rater.DEFAULT_MODEL, species, pool),
        dry_run=options.get("dry_run", False),
        limit=options.get("limit"),
        out=printed.append,
    )
    return tally, printed


_ITEM = {"images": ["Stored.jpg"], "category": "Species 1", "files": ["Better_one.jpg", "Map.svg", "Other.png"]}


class TestTitles:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("File:Pterois_miles 1.jpg", "Pterois miles 1.jpg"),
            ("  lionfish.JPEG ", "Lionfish.JPEG"),
            ("Range map.svg", None),
            ("", None),
        ],
    )
    def test_normalizes_as_the_instance_stores_a_title(self, raw: str, expected: str | None):
        assert rater.normalize_title(raw) == expected


class TestPool:
    def test_gathers_item_images_stored_photo_and_category_raster_files_marking_the_stored(self):
        wikimedia = FakeWikimedia({"Q1": _ITEM})
        pool = rater.gather_pool(wikimedia.client(), qid="Q1", stored="Stored.jpg")
        assert [file.title for file in pool] == ["Stored.jpg", "Better one.jpg", "Other.png"]
        assert [file.is_stored for file in pool] == [True, False, False]
        assert pool[1].width == 4000 and pool[1].data == _JPEG

    def test_cuts_the_pool_at_twenty_four(self):
        files = [f"Photo {n}.jpg" for n in range(40)]
        wikimedia = FakeWikimedia({"Q1": {"category": "Big", "files": files}})
        pool = rater.gather_pool(wikimedia.client(), qid="Q1", stored=None)
        assert len(pool) == 24

    def test_asks_wikimedia_in_series_under_a_user_agent_with_the_contact(self):
        wikimedia = FakeWikimedia({"Q1": _ITEM})
        rater.gather_pool(wikimedia.client(), qid="Q1", stored="Stored.jpg")
        agents = {request.headers["user-agent"] for request in wikimedia.requests}
        assert agents == {rater.wikimedia_user_agent("ops@example.org")}
        assert "ops@example.org" in agents.pop()

    def test_an_unreadable_wikimedia_is_no_pool(self):
        wikimedia = FakeWikimedia({"Q1": _ITEM})
        wikimedia.broken.add("commons.wikimedia.org")
        assert rater.gather_pool(wikimedia.client(), qid="Q1", stored="Stored.jpg") is None


class TestRun:
    def test_proposes_the_chosen_file_with_the_reason_as_its_note(self):
        instance = FakeInstance([_species("1")])
        model = FakeModel(_message({"choice": "Better one.jpg", "reason": "Whole animal, in the water."}))
        tally, printed = _run(instance, FakeWikimedia({"Q1": _ITEM}), model)
        assert instance.posted == [("1", {"file": "Better one.jpg", "note": "Whole animal, in the water."})]
        assert tally.proposed == 1
        assert tally.line(dry_run=False).startswith("Species read: 1;")

    def test_sends_names_titles_sizes_and_the_stored_mark_with_each_image_as_bytes(self):
        instance = FakeInstance([_species("1")])
        model = FakeModel(_message({"choice": "none", "reason": "No better."}))
        _run(instance, FakeWikimedia({"Q1": _ITEM}), model)
        request = model.requests[0]
        assert request["model"] == "claude-opus-5-5"
        content = request["messages"][0]["content"]
        assert "Species 1 (common 1)" in content[0]["text"]
        assert "clearly better than the stored photo" in content[0]["text"]
        labels = [block["text"] for block in content[1:] if block["type"] == "text"]
        assert labels[0] == "Stored.jpg (4000 x 3000 px) - the stored photo"
        images = [block for block in content if block["type"] == "image"]
        assert len(images) == 3
        assert {image["source"]["type"] for image in images} == {"base64"}
        schema = request["output_config"]["format"]["schema"]
        assert schema["properties"]["choice"]["enum"] == ["Stored.jpg", "Better one.jpg", "Other.png", "none"]

    def test_a_species_with_no_stored_photo_is_judged_against_the_bar_alone(self):
        instance = FakeInstance([_species("1", photo_file=None)])
        model = FakeModel(_message({"choice": "Other.png", "reason": "Best of them."}))
        _run(instance, FakeWikimedia({"Q1": {"category": "Species 1", "files": ["Other.png"]}}), model)
        text = model.requests[0]["messages"][0]["content"][0]["text"]
        assert "has no photo yet" in text and "stored photo" not in text
        assert instance.posted == [("1", {"file": "Other.png", "note": "Best of them."})]

    def test_skips_species_settled_by_this_account_without_reading_them(self):
        proposals = [
            {"species": {"uuid": "1"}, "status": "waiting"},
            {"species": {"uuid": "2"}, "status": "accepted"},
            {"species": {"uuid": "3"}, "status": "declined"},
            {"species": {"uuid": "4"}, "status": "withdrawn"},
            {"species": {"uuid": "4"}, "status": "closed"},
        ]
        instance = FakeInstance([_species(n) for n in "1234"], proposals=proposals)
        wikimedia = FakeWikimedia({"Q4": {**_ITEM, "category": "Four"}})
        model = FakeModel(_message({"choice": "none", "reason": "-"}))
        tally, _ = _run(instance, wikimedia, model)
        assert instance.species_reads == ["4"]
        assert tally.skipped == {"already proposed": 3}
        assert tally.answered_none == 1

    def test_skips_without_asking_wikimedia_a_species_the_operator_decided_or_with_no_item(self):
        instance = FakeInstance([_species("1", accepts_photo_proposals=False), _species("2", wikidata_qid=None)])
        wikimedia = FakeWikimedia({})
        model = FakeModel()
        tally, _ = _run(instance, wikimedia, model)
        assert wikimedia.requests == [] and model.requests == []
        assert tally.skipped == {"photo chosen by the operator": 1, "no Wikidata item": 1}

    def test_skips_without_a_model_call_a_pool_holding_only_the_stored_photo(self):
        instance = FakeInstance([_species("1")])
        model = FakeModel()
        tally, _ = _run(instance, FakeWikimedia({"Q1": {"images": ["Stored.jpg"]}}), model)
        assert model.requests == []
        assert tally.skipped == {"nothing to compare": 1}

    def test_reports_and_skips_a_species_whose_pool_cannot_be_read(self):
        instance = FakeInstance([_species("1")])
        wikimedia = FakeWikimedia({"Q1": _ITEM})
        wikimedia.broken.add("www.wikidata.org")
        tally, printed = _run(instance, wikimedia, FakeModel())
        assert tally.skipped == {"pool unreadable": 1}
        assert any("could not be read" in line for line in printed)

    @pytest.mark.parametrize(
        "message",
        [
            _message({"choice": "Better one.jpg", "reason": "x"}, stop_reason="refusal"),
            _message('{"choice": "Better one', stop_reason="max_tokens"),
            _message("not json"),
            _message({"reason": "no choice"}),
            _message({"choice": "Never shown.jpg", "reason": "x"}),
        ],
    )
    def test_an_answer_it_cannot_use_proposes_nothing_and_is_reported(self, message: SimpleNamespace):
        instance = FakeInstance([_species("1")])
        tally, printed = _run(instance, FakeWikimedia({"Q1": _ITEM}), FakeModel(message))
        assert instance.posted == []
        assert tally.skipped == {"no usable answer": 1}
        assert any("nothing proposed" in line for line in printed)

    def test_choosing_the_stored_photo_is_answering_none(self):
        instance = FakeInstance([_species("1")])
        tally, _ = _run(
            instance, FakeWikimedia({"Q1": _ITEM}), FakeModel(_message({"choice": "Stored.jpg", "reason": "x"}))
        )
        assert instance.posted == [] and tally.answered_none == 1

    def test_a_dry_run_proposes_nothing_and_prints_what_it_would(self):
        instance = FakeInstance([_species("1")])
        model = FakeModel(_message({"choice": "Better one.jpg", "reason": "Clearer."}))
        tally, printed = _run(instance, FakeWikimedia({"Q1": _ITEM}), model, dry_run=True)
        assert instance.posted == []
        assert printed == ["Species 1: would propose Better one.jpg - Clearer."]
        assert "would propose: 1" in tally.line(dry_run=True)

    def test_the_limit_caps_the_species_rated(self):
        instance = FakeInstance([_species(n) for n in "123"])
        items = {f"Q{n}": {**_ITEM, "category": f"C{n}"} for n in "123"}
        model = FakeModel(*[_message({"choice": "none", "reason": "-"}) for _ in range(3)])
        _run(instance, FakeWikimedia(items), model, limit=2)
        assert len(model.requests) == 2

    def test_a_refusal_is_counted_and_the_run_continues_with_the_note_cut_to_500(self):
        instance = FakeInstance([_species("1"), _species("2")], propose_status={"1": 409})
        items = {"Q1": _ITEM, "Q2": {**_ITEM, "category": "C2"}}
        long_reason = "x" * 900
        model = FakeModel(
            _message({"choice": "Better one.jpg", "reason": long_reason}),
            _message({"choice": "Other.png", "reason": "ok"}),
        )
        tally, printed = _run(instance, FakeWikimedia(items), model)
        assert len(instance.posted[0][1]["note"]) == 500
        assert tally.refused == 1 and tally.proposed == 1
        assert any("refused, 409" in line for line in printed)

    def test_stops_cleanly_on_the_instance_daily_limit(self):
        instance = FakeInstance([_species("1"), _species("2")], propose_status={"1": 429})
        items = {"Q1": _ITEM, "Q2": {**_ITEM, "category": "C2"}}
        model = FakeModel(
            _message({"choice": "Better one.jpg", "reason": "x"}), _message({"choice": "none", "reason": "-"})
        )
        tally, printed = _run(instance, FakeWikimedia(items), model)
        assert len(model.requests) == 1
        assert "SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_PER_USER" in printed[-1]
        assert "Run again later" in printed[-1]
        assert tally.proposed == 0

    def test_pages_the_life_list(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rater, "_PAGE_SIZE", 2)
        instance = FakeInstance([_species(n, accepts_photo_proposals=False) for n in "12345"])
        tally, _ = _run(instance, FakeWikimedia({}), FakeModel())
        assert tally.read == 5


class TestMain:
    def test_exits_zero_after_the_summary_line_on_the_daily_limit_and_never_prints_a_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        instance = FakeInstance([_species("1")], propose_status={"1": 429})
        wikimedia = FakeWikimedia({"Q1": _ITEM})
        model = FakeModel(_message({"choice": "Better one.jpg", "reason": "x"}))
        real_client = httpx.Client

        def client(**options: Any) -> httpx.Client:
            handler = instance.handle if options.get("base_url") else wikimedia.handle
            return real_client(**options, transport=httpx.MockTransport(handler))

        monkeypatch.setenv("OPENDIVING_API_KEY", _KEY)
        monkeypatch.setattr(rater.httpx, "Client", client)
        monkeypatch.setattr(rater.anthropic, "Anthropic", lambda: model)
        status = rater.main(["--instance", "http://instance.test/", "--contact", "ops@example.org"])
        out = capsys.readouterr()
        assert status == 0
        assert out.out.splitlines()[-1].startswith("Species read: 1;")
        assert _KEY not in out.out + out.err
        assert instance.authorizations == {f"Bearer {_KEY}"}

    def test_refuses_to_start_without_a_key(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENDIVING_API_KEY", raising=False)
        assert rater.main(["--instance", "http://instance.test", "--contact", "ops@example.org"]) == 2

    def test_an_instance_error_stops_the_run_with_a_failing_status(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        real_client = httpx.Client

        def client(**options: Any) -> httpx.Client:
            return real_client(**options, transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})))

        monkeypatch.setenv("OPENDIVING_API_KEY", _KEY)
        monkeypatch.setattr(rater.httpx, "Client", client)
        monkeypatch.setattr(rater.anthropic, "Anthropic", lambda: FakeModel())
        assert rater.main(["--instance", "http://instance.test", "--contact", "ops@example.org"]) == 1
        out = capsys.readouterr()
        assert "answered 401" in out.err
        assert out.out.startswith("Species read: 0;")
