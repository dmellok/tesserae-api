"""The review embed for a template submission.

``submission_embed`` is pure, so these call it directly with row dicts rather
than going through the submit endpoint. The focus is the inputs list: a
reviewer approving a template is approving what it will ask a stranger for on
install, and the embed must survive whatever ``inputs_spec`` holds without
raising (a notification that throws costs a review message).
"""

from __future__ import annotations

import json

import pytest

from tesserae_api.config import Settings
from tesserae_api.discord_client import _inputs_summary, submission_embed

# Discord's per-field value cap.
_FIELD_LIMIT = 1024


def _input(name: str, **over) -> dict:
    """One entry shaped as the submitting server normalises them."""
    spec = {
        "name": name,
        "label": f"Label for {name}",
        "type": "string",
        "secret": False,
        "required": False,
        "default": "",
        "choices": [],
        "targets": [{"el": "e1", "slot": "options", "index": 0, "key": "url"}],
    }
    spec.update(over)
    return spec


def _row(inputs: list[dict]) -> dict:
    return {
        "id": "abc123",
        "slug": "board-abc123",
        "title": "Bin Day",
        "description": "",
        "w": 800,
        "h": 480,
        "tags": "[]",
        "requires": "[]",
        "lint": "{}",
        "tesserae_version": "0.315.0",
        "inputs_count": len(inputs),
        "inputs_spec": json.dumps(inputs),
    }


def _field(embed: dict, name: str) -> str:
    return next(f["value"] for f in embed["fields"] if f["name"] == name)


def test_lists_each_input_with_name_type_and_label(settings: Settings):
    embed = submission_embed(
        settings,
        _row([_input("bin_api", label="Bin collection API URL"), _input("city", type="select")]),
    )
    value = _field(embed, "Asks the installer for")
    assert "bin_api" in value
    assert "(string)" in value
    assert "Bin collection API URL" in value
    assert "city" in value
    assert "(select)" in value
    assert len(value.splitlines()) == 2


def test_count_field_survives(settings: Settings):
    """A reviewer scanning the channel still gets the number at a glance."""
    embed = submission_embed(settings, _row([_input("a"), _input("b"), _input("c")]))
    assert _field(embed, "Inputs") == "3"


def test_secret_input_is_marked(settings: Settings):
    value = _inputs_summary(json.dumps([_input("api_key", secret=True), _input("city")]))
    secret_line, plain_line = value.splitlines()
    assert secret_line.startswith("🔒")
    assert "secret" in secret_line
    assert not plain_line.startswith("🔒")
    assert "secret" not in plain_line


def test_required_and_request_shaping_slots_are_named(settings: Settings):
    """``source_header``/``source_url`` mean the input shapes an outbound
    request, which is the thing a preview image cannot show."""
    value = _inputs_summary(
        json.dumps(
            [
                _input(
                    "auth",
                    required=True,
                    secret=True,
                    targets=[
                        {"el": "e1", "slot": "source_header", "index": 0, "key": "Authorization"},
                        {"el": "e1", "slot": "source_url", "index": 0, "key": "url"},
                    ],
                )
            ]
        )
    )
    assert "required" in value
    assert "request header" in value
    assert "request URL" in value


def test_ordinary_slots_are_not_called_out(settings: Settings):
    value = _inputs_summary(json.dumps([_input("city")]))
    assert "request" not in value
    assert value.startswith("• city (string)")


def test_zero_inputs_reads_as_normal(settings: Settings):
    """A template that asks nothing is the common case, not an error."""
    embed = submission_embed(settings, _row([]))
    value = _field(embed, "Asks the installer for")
    assert value == "none (installs without asking anything)"
    assert _field(embed, "Inputs") == "0"


@pytest.mark.parametrize(
    "spec",
    ["not json at all", "{", '{"name": "x"}', '"a string"', "42"],
)
def test_malformed_inputs_spec_degrades_without_raising(settings: Settings, spec: str):
    row = _row([])
    row["inputs_spec"] = spec
    value = _field(submission_embed(settings, row), "Asks the installer for")
    assert value == "inputs list unreadable"


def test_missing_inputs_spec_is_treated_as_none(settings: Settings):
    """Rows predating the column, and any row where the write was skipped."""
    row = _row([])
    row["inputs_spec"] = None
    assert _field(submission_embed(settings, row), "Asks the installer for").startswith("none")
    del row["inputs_spec"]
    assert _field(submission_embed(settings, row), "Asks the installer for").startswith("none")


def test_entries_of_the_wrong_shape_still_render(settings: Settings):
    value = _inputs_summary(json.dumps(["bare string", 7, None, _input("ok")]))
    assert "unreadable entry" in value
    assert "ok" in value


def test_missing_keys_do_not_raise(settings: Settings):
    value = _inputs_summary(json.dumps([{}, {"name": "half"}]))
    assert "?" in value
    assert "half" in value


def test_more_inputs_than_the_cap_truncate_with_a_remainder(settings: Settings):
    value = _inputs_summary(json.dumps([_input(f"input_{i}") for i in range(20)]))
    lines = value.splitlines()
    assert lines[-1] == f"…and {20 - (len(lines) - 1)} more"
    assert sum(int(line.startswith("…")) for line in lines) == 1


def test_maximal_submission_stays_within_the_field_limit(settings: Settings):
    """20 inputs (the submit cap) with 32-char names and 80-char labels: the
    case that would silently fail in production if the list were unbounded."""
    inputs = [
        _input(("i" * 29) + f"{i:03d}", label="L" * 80, secret=True, required=True)
        for i in range(20)
    ]
    embed = submission_embed(settings, _row(inputs))
    value = _field(embed, "Asks the installer for")
    assert len(value) <= _FIELD_LIMIT
    assert value.splitlines()[-1].startswith("…and ")


def test_an_overlong_label_cannot_blow_the_budget(settings: Settings):
    """``label`` is not length-validated on submit, so the embed clamps it."""
    value = _inputs_summary(json.dumps([_input("wordy", label="x" * 5000)]))
    assert len(value) <= _FIELD_LIMIT
    assert "x" * 81 not in value


def test_every_field_value_fits_discords_caps(settings: Settings):
    embed = submission_embed(settings, _row([_input(f"input_{i}") for i in range(20)]))
    assert len(embed["fields"]) <= 25
    assert all(len(f["value"]) <= _FIELD_LIMIT for f in embed["fields"])
