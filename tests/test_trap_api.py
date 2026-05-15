"""Integration tests for POST /v1/trap/apply."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from compile_pdf.api.main import app


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_trap_apply_round_trips(simple_pdf: bytes) -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/trap/apply",
        json={
            "input_pdf_b64": _b64(simple_pdf),
            "policy": {
                "default_trap_width_pt": 0.5,
                "trap_zones": [
                    {
                        "page_index": 0,
                        "rect_pt": [100, 100, 300, 300],
                        "from_ink": "Y",
                        "to_ink": "K",
                    }
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"] == "pure_python"
    assert body["operations_count"] == 1
    assert body["engine_fingerprint"].startswith("pure_python@")
    assert body["trap_diff"]["operations"][0]["from_ink"] == "Y"
    assert body["pdf_sha256"]
    assert body["cache_key"]


def test_trap_apply_rejects_invalid_base64() -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/trap/apply",
        json={"input_pdf_b64": "not-valid!!!", "policy": {}},
    )
    assert response.status_code == 400


def test_trap_apply_rejects_unknown_field(simple_pdf: bytes) -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/trap/apply",
        json={"input_pdf_b64": _b64(simple_pdf), "policy": {"bogus": True}},
    )
    assert response.status_code == 422


def test_trap_apply_rejects_zone_referencing_missing_page(simple_pdf: bytes) -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/trap/apply",
        json={
            "input_pdf_b64": _b64(simple_pdf),
            "policy": {
                "trap_zones": [
                    {
                        "page_index": 99,
                        "rect_pt": [0, 0, 10, 10],
                        "from_ink": "Y",
                        "to_ink": "K",
                    }
                ]
            },
        },
    )
    assert response.status_code == 422


def test_contract_endpoint_lists_trap() -> None:
    client = TestClient(app)
    response = client.get("/v1/contract")
    assert response.status_code == 200
    endpoints = response.json()["endpoints"]
    assert any("/v1/trap/apply" in e for e in endpoints)


def test_same_input_same_policy_same_cache_key(simple_pdf: bytes) -> None:
    client = TestClient(app)
    payload = {
        "input_pdf_b64": _b64(simple_pdf),
        "policy": {
            "trap_zones": [
                {
                    "page_index": 0,
                    "rect_pt": [50, 50, 100, 100],
                    "from_ink": "C",
                    "to_ink": "M",
                }
            ]
        },
    }
    a = client.post("/v1/trap/apply", json=payload).json()
    b = client.post("/v1/trap/apply", json=payload).json()
    assert a["cache_key"] == b["cache_key"]
    assert a["pdf_sha256"] == b["pdf_sha256"]
