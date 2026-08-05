from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas import EvaluationRequest
from tests.conftest import payload


def request_payload(**updates):
    """Return an isolated payload so parameterized cases cannot leak mutations."""
    return deepcopy(payload(**updates))


@pytest.mark.parametrize("request_id", ["a", "Z", "0", ".", "_", ":", "-", "Az09._:-"])
def test_request_id_accepts_documented_characters(request_id: str):
    request = EvaluationRequest.model_validate(request_payload(request_id=request_id))

    assert request.request_id == request_id


@pytest.mark.parametrize("request_id", ["has space", "has/slash", "fare@test", "测试"])
def test_request_id_rejects_characters_outside_contract(request_id: str):
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(request_payload(request_id=request_id))


@pytest.mark.parametrize(
    ("length", "is_valid"),
    [
        pytest.param(0, False, id="empty"),
        pytest.param(1, True, id="minimum"),
        pytest.param(200, True, id="maximum"),
        pytest.param(201, False, id="over-maximum"),
    ],
)
def test_request_id_length_boundaries(length: int, is_valid: bool):
    value = request_payload(request_id="a" * length)

    if is_valid:
        request = EvaluationRequest.model_validate(value)
        assert len(request.request_id) == length
    else:
        with pytest.raises(ValidationError):
            EvaluationRequest.model_validate(value)


def _list_value(field: str, size: int) -> list[dict[str, object]]:
    if field in {"sources", "destinations"}:
        item: dict[str, object] = {"address": "192.0.2.1", "description": "boundary"}
    else:
        item = {"start": 443, "end": 443}
    return [deepcopy(item) for _ in range(size)]


@pytest.mark.parametrize("field", ["sources", "destinations", "ports"])
@pytest.mark.parametrize(
    ("size", "is_valid"),
    [
        pytest.param(0, False, id="empty"),
        pytest.param(1, True, id="minimum"),
        pytest.param(100, True, id="maximum"),
        pytest.param(101, False, id="over-maximum"),
    ],
)
def test_request_list_length_boundaries(field: str, size: int, is_valid: bool):
    value = request_payload(**{field: _list_value(field, size)})

    if is_valid:
        request = EvaluationRequest.model_validate(value)
        assert len(getattr(request, field)) == size
    else:
        with pytest.raises(ValidationError):
            EvaluationRequest.model_validate(value)


@pytest.mark.parametrize(
    "port",
    [
        pytest.param({"start": 0, "end": 0}, id="zero"),
        pytest.param({"start": 65535, "end": 65535}, id="maximum"),
        pytest.param({"start": 0, "end": 65535}, id="full-range"),
    ],
)
def test_port_boundaries_are_valid(port: dict[str, int]):
    request = EvaluationRequest.model_validate(request_payload(ports=[port]))

    assert request.ports[0].start == port["start"]
    assert request.ports[0].end == port["end"]


@pytest.mark.parametrize(
    "port",
    [
        pytest.param({"start": -1, "end": 443}, id="start-below-minimum"),
        pytest.param({"start": 443, "end": 65536}, id="end-above-maximum"),
        pytest.param({"start": 444, "end": 443}, id="reversed"),
    ],
)
def test_invalid_port_ranges_are_rejected(port: dict[str, int]):
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(request_payload(ports=[port]))


def test_protocol_is_normalized_to_lowercase():
    request = EvaluationRequest.model_validate(request_payload(protocol="TCP"))

    assert request.protocol == "tcp"


@pytest.mark.parametrize(
    ("target", "expected_location"),
    [
        pytest.param("request", ("unexpected",), id="top-level"),
        pytest.param("address", ("sources", 0, "unexpected"), id="address"),
        pytest.param("port", ("ports", 0, "unexpected"), id="port"),
    ],
)
def test_extra_fields_are_rejected(target: str, expected_location: tuple[object, ...]):
    value = request_payload()
    if target == "request":
        value["unexpected"] = True
    elif target == "address":
        value["sources"][0]["unexpected"] = True
    else:
        value["ports"][0]["unexpected"] = True

    with pytest.raises(ValidationError) as exc_info:
        EvaluationRequest.model_validate(value)

    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == expected_location
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("not-an-address", id="hostname"),
        pytest.param("999.1.1.1", id="invalid-ipv4"),
        pytest.param("10.0.0.1/33", id="invalid-prefix"),
    ],
)
def test_invalid_addresses_are_rejected(address: str):
    value = request_payload(sources=[{"address": address, "description": "invalid"}])

    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate(value)
