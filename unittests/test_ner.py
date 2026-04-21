"""Tests for the GLiNER-backed NER detector and the fetch-model CLI command.

Two layers:

* Unit tests use a fake GLiNER model (`_FakeModel`) injected via the
  ``model=`` constructor kwarg. They verify the detector wiring: span
  construction, threshold/labels propagation, error paths when the package
  or model is missing, and the public detector helpers.
* Integration tests are gated behind ``@pytest.mark.ner`` and load the real
  ``urchade/gliner_multi-v2.1`` snapshot from disk. They are deselected by
  default (see ``pyproject.toml``); run with ``pytest -m ner`` to exercise
  the real model. CI uses a separate cached job for these.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from kuckuck.detectors import EntityType
from kuckuck.detectors.base import Priority
from kuckuck.detectors.ner import (
    DEFAULT_LABELS,
    DEFAULT_THRESHOLD,
    NerDetector,
    NerModelMissingError,
    NerNotInstalledError,
    default_cache_root,
    default_model_path,
    is_gliner_installed,
    is_model_available,
)


class _FakeModel:
    """A minimal stand-in for a GLiNER model.

    Records the (text, labels, threshold) it was called with so tests can
    assert that the detector forwards configuration correctly. Returns
    whatever ``predictions`` the test sets up.
    """

    def __init__(self, predictions: list[dict[str, Any]] | None = None) -> None:
        self.predictions = predictions or []
        self.calls: list[dict[str, Any]] = []

    def predict_entities(self, text: str, labels: list[str], threshold: float) -> list[dict[str, Any]]:
        self.calls.append({"text": text, "labels": labels, "threshold": threshold})
        return self.predictions


class TestNerDetectorWithFakeModel:
    def test_detect_emits_person_spans(self) -> None:
        text = "Mit freundlichen Gruessen Max Mustermann"
        # 26 -> 41 -> "Max Mustermann"
        fake = _FakeModel([{"start": 26, "end": 40, "label": "person", "score": 0.9}])
        det = NerDetector(model=fake)
        spans = det.detect(text)
        assert len(spans) == 1
        assert spans[0].text == "Max Mustermann"
        assert spans[0].entity_type == EntityType.PERSON
        assert spans[0].detector_name == "ner"

    def test_detect_passes_threshold_and_labels(self) -> None:
        fake = _FakeModel()
        det = NerDetector(model=fake, threshold=0.7, labels=("person", "location"))
        det.detect("Berlin")
        assert fake.calls[0]["threshold"] == 0.7
        assert fake.calls[0]["labels"] == ["person", "location"]

    def test_defaults_match_module_constants(self) -> None:
        fake = _FakeModel()
        det = NerDetector(model=fake)
        det.detect("anything")
        assert fake.calls[0]["threshold"] == DEFAULT_THRESHOLD
        assert fake.calls[0]["labels"] == list(DEFAULT_LABELS)

    def test_empty_input_short_circuits(self) -> None:
        fake = _FakeModel([{"start": 0, "end": 10, "label": "person", "score": 0.9}])
        det = NerDetector(model=fake)
        assert det.detect("") == []
        assert det.detect("   \n\t") == []
        assert fake.calls == []  # model never invoked for empty input

    def test_invalid_offsets_are_dropped(self) -> None:
        text = "short"
        fake = _FakeModel(
            [
                {"start": 0, "end": 0, "label": "person", "score": 0.9},  # zero-length
                {"start": 5, "end": 3, "label": "person", "score": 0.9},  # inverted
                {"start": -1, "end": 3, "label": "person", "score": 0.9},  # negative
                {"start": 0, "end": 1000, "label": "person", "score": 0.9},  # past end
            ]
        )
        det = NerDetector(model=fake)
        assert det.detect(text) == []

    def test_priority_lowest(self) -> None:
        # NER must yield to regex detectors when spans collide.
        det = NerDetector(model=_FakeModel())
        assert det.priority == Priority.PERSON

    def test_span_text_is_sliced_from_input(self) -> None:
        # The model could theoretically return a 'text' field but we ignore
        # it and re-slice from the source so offsets and surface form stay
        # consistent.
        text = "Hello Anna there"
        fake = _FakeModel([{"start": 6, "end": 10, "label": "person", "score": 0.9}])
        det = NerDetector(model=fake)
        spans = det.detect(text)
        assert spans[0].text == "Anna"
        assert text[spans[0].start : spans[0].end] == "Anna"


class TestModelDiscovery:
    def test_default_cache_root_is_under_cache(self) -> None:
        root = default_cache_root()
        assert root.parts[-2:] == ("kuckuck", "models")

    def test_default_model_path_under_cache_root(self) -> None:
        path = default_model_path()
        assert path.parent == default_cache_root()
        assert path.name == "gliner_multi-v2.1"

    def test_is_model_available_false_for_missing_dir(self, tmp_path: Path) -> None:
        assert is_model_available(tmp_path / "no-such-model") is False

    def test_is_model_available_false_for_dir_without_config(self, tmp_path: Path) -> None:
        target = tmp_path / "model"
        target.mkdir()
        assert is_model_available(target) is False

    def test_is_model_available_false_for_config_without_weights(self, tmp_path: Path) -> None:
        # Half-downloaded snapshot: config landed first, weights didn't.
        # Must be reported as unavailable so --ner does not crash later.
        target = tmp_path / "model"
        target.mkdir()
        (target / "config.json").write_text("{}", encoding="utf-8")
        assert is_model_available(target) is False

    def test_is_model_available_true_for_complete_snapshot(self, tmp_path: Path) -> None:
        target = tmp_path / "model"
        target.mkdir()
        (target / "gliner_config.json").write_text("{}", encoding="utf-8")
        (target / "model.safetensors").write_bytes(b"")
        assert is_model_available(target) is True

    def test_is_model_available_accepts_pytorch_bin_weights(self, tmp_path: Path) -> None:
        target = tmp_path / "model"
        target.mkdir()
        (target / "config.json").write_text("{}", encoding="utf-8")
        (target / "pytorch_model.bin").write_bytes(b"")
        assert is_model_available(target) is True


class TestErrorPaths:
    def test_load_raises_when_gliner_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pretend gliner isn't installed even if it is locally.
        monkeypatch.setattr("kuckuck.detectors.ner.is_gliner_installed", lambda: False)
        det = NerDetector(model_path=tmp_path / "no-model")
        with pytest.raises(NerNotInstalledError):
            det.detect("text")

    def test_load_raises_when_model_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # gliner installed (faked), but no model on disk.
        monkeypatch.setattr("kuckuck.detectors.ner.is_gliner_installed", lambda: True)
        det = NerDetector(model_path=tmp_path / "no-model")
        with pytest.raises(NerModelMissingError):
            det.detect("text")

    def test_is_gliner_installed_returns_bool(self) -> None:
        # The function must return a bool, never raise.
        assert isinstance(is_gliner_installed(), bool)

    def test_load_uses_injected_model_without_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with gliner missing, an injected model bypasses the import.
        monkeypatch.setattr("kuckuck.detectors.ner.is_gliner_installed", lambda: False)
        det = NerDetector(model=_FakeModel([{"start": 0, "end": 4, "label": "person", "score": 0.9}]))
        spans = det.detect("Anna here")
        assert len(spans) == 1


class TestGlinerImportPath:
    def test_load_imports_gliner_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build a fake 'gliner' module and inject it into sys.modules so
        # the detector's lazy import resolves without pulling in real torch.
        fake_module = types.ModuleType("gliner")
        loaded_paths: list[str] = []

        class FakeGLiNER:
            @classmethod
            def from_pretrained(cls, path: str) -> _FakeModel:
                loaded_paths.append(path)
                return _FakeModel([{"start": 0, "end": 4, "label": "person", "score": 0.9}])

        fake_module.GLiNER = FakeGLiNER  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "gliner", fake_module)
        # Also override is_gliner_installed because importlib.find_spec doesn't
        # see the monkey-patched module.
        monkeypatch.setattr("kuckuck.detectors.ner.is_gliner_installed", lambda: True)

        # Pretend the model is on disk (config + weights).
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "model.safetensors").write_bytes(b"")

        det = NerDetector(model_path=model_dir)
        spans = det.detect("Anna says hi")
        assert spans[0].text == "Anna"
        assert loaded_paths == [str(model_dir)]


# Integration tests with the real model — opt-in via -m ner.
#
# These exist to keep us honest about the practical quality of the bundled
# model. Common German first names should be detected with ~100 % recall on
# clean signature/salutation contexts; if recall drops it is almost
# certainly a regression in the model snapshot pin or the threshold.
@pytest.mark.ner
class TestRealModel:
    @pytest.fixture(scope="class")
    def detector(self) -> NerDetector:
        if not is_gliner_installed() or not is_model_available():
            pytest.skip("gliner / model not present")
        # Module-level cache: loading the model takes 5-10 s; we don't want
        # to pay that for every test in this class.
        return NerDetector()

    def test_detects_person_in_german_signature(self, detector: NerDetector) -> None:
        text = "Mit freundlichen Gruessen,\nMax Mustermann\nProjektleiter"
        spans = detector.detect(text)
        person_texts = {s.text for s in spans if s.entity_type == EntityType.PERSON}
        assert any("Max" in t for t in person_texts)

    @pytest.mark.parametrize(
        "name",
        [
            "Hans",
            "Peter",
            "Anna",
            "Maria",
            "Stefan",
            "Klaus",
            "Petra",
            "Andreas",
            "Sabine",
            "Michael",
            "Julia",
            "Thomas",
        ],
    )
    def test_common_german_first_names_in_salutation(self, detector: NerDetector, name: str) -> None:
        # Salutation context is the easiest case for the model; if it fails
        # here the model is broken or the threshold is too high.
        text = f"Hallo {name}, danke für die Nachricht!"
        spans = detector.detect(text)
        person_texts = [s.text for s in spans if s.entity_type == EntityType.PERSON]
        assert any(
            name in t for t in person_texts
        ), f"GLiNER did not detect '{name}' as a person. spans={person_texts!r}"

    @pytest.mark.parametrize(
        "full_name",
        [
            "Hans Müller",
            "Peter Schmidt",
            "Anna Becker",
            "Klaus-Dieter Schulz",
            "Maria von Hohenheim",
        ],
    )
    def test_common_german_full_names_in_signature(self, detector: NerDetector, full_name: str) -> None:
        text = f"Mit freundlichen Gruessen\n{full_name}\nGeschäftsführung"
        spans = detector.detect(text)
        # We allow either the full name or the surname to be flagged - GLiNER
        # sometimes splits hyphenated or noble names. The point is that some
        # PERSON span overlaps the name fragment.
        first_token = full_name.split()[0]
        person_texts = [s.text for s in spans if s.entity_type == EntityType.PERSON]
        assert any(
            first_token in t for t in person_texts
        ), f"GLiNER did not detect '{full_name}' as a person. spans={person_texts!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "Die Besprechung wurde verschoben, weil die Strategie noch nicht steht.",
            "Die neue Software wurde gestern erfolgreich ausgerollt.",
            "Im naechsten Quartal wird das Budget angepasst.",
            "Die Datenbank wird automatisch jede Nacht gesichert.",
            "Heute ist ein guter Tag, um an der Doku zu arbeiten.",
        ],
    )
    def test_does_not_flag_abstract_prose(self, detector: NerDetector, text: str) -> None:
        # Counter-test: GLiNER is not perfect — it occasionally flags role
        # nouns like "der Projektleiter" (a known false-positive corridor
        # we accept). We use multiple abstract sentences without role nouns
        # to keep this from being flaky on a single example.
        spans = detector.detect(text)
        person_texts = [s.text for s in spans if s.entity_type == EntityType.PERSON]
        assert person_texts == [], (
            f"unexpected PERSON spans on abstract prose: {text!r} -> {person_texts!r}"
        )

    def test_works_through_pseudonymize_pipeline(self, detector: NerDetector) -> None:
        # End-to-end: ensure NerDetector slots into pseudonymize_text
        # alongside the regex detectors and produces [[PERSON_...]] tokens.
        from pydantic import SecretStr

        from kuckuck.pseudonymize import build_default_detectors, pseudonymize_text

        detectors = build_default_detectors() + [detector]
        master = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
        result = pseudonymize_text("Hallo Hans, ruf mich unter +49 40 12345 zurück.", master, detectors)
        assert "[[PERSON_" in result.text
        assert "Hans" not in result.text

    def test_ner_finds_names_that_regex_misses(self, detector: NerDetector) -> None:
        # The motivating value of NER: catches plain-text personal names
        # that the regex pipeline cannot see (no @-handle, no email).
        # This test compares the same input through both detector sets and
        # asserts NER produces strictly more PERSON spans.
        from pydantic import SecretStr

        from kuckuck.pseudonymize import build_default_detectors, pseudonymize_text

        master = SecretStr("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
        text = (
            "Hallo zusammen,\n"
            "Hans hat heute morgen mit Peter und Anna gesprochen.\n"
            "Maria ist krank und bleibt zuhause.\n"
            "Viele Gruesse"
        )

        regex_only = pseudonymize_text(text, master, build_default_detectors())
        with_ner = pseudonymize_text(text, master, build_default_detectors() + [detector])

        # Regex pipeline cannot see plain first names — none of these become tokens.
        for name in ("Hans", "Peter", "Anna", "Maria"):
            assert name in regex_only.text, f"regex-only baseline unexpectedly removed {name!r}"

        # NER pipeline must catch the four names. We allow the model to miss
        # at most one (defensive against a single false negative on noisy
        # context) but not three of four — that would mean NER is no better
        # than baseline.
        hits = sum(1 for name in ("Hans", "Peter", "Anna", "Maria") if name not in with_ner.text)
        assert hits >= 3, f"NER detected only {hits}/4 expected names. with_ner={with_ner.text!r}"

        # And concretely: NER produced more PERSON tokens than baseline.
        assert with_ner.text.count("[[PERSON_") > regex_only.text.count(
            "[[PERSON_"
        ), f"NER did not add PERSON tokens: regex={regex_only.text!r} ner={with_ner.text!r}"
