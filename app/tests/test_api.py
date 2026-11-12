"""API-level tests for /analyze."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SAMPLE = {
    "sources": [
        {"name": "waybills",
         "columns": ["waybill_id", "receiver_zip_old", "city_id", "weight_kg"]},
        {"name": "dim_delivery_zone",
         "columns": ["zone_id", "zip_code", "zone_name"]},
    ],
    "models": [
        {
            "name": "stg_waybill",
            "sql": ("WITH cleaned AS ("
                    " SELECT waybill_id, receiver_zip_old AS zip, city_id, weight_kg"
                    " FROM waybills WHERE receiver_zip_old IS NOT NULL"
                    ") SELECT * FROM cleaned"),
        },
        {
            "name": "dwd_delivery_fact",
            "sql": ("SELECT s.waybill_id, s.zip AS receiver_zip, z.zone_name, s.weight_kg"
                    " FROM stg_waybill s JOIN dim_delivery_zone z ON s.zip = z.zip_code"
                    " WHERE s.zip LIKE '10%'"),
        },
    ],
    "targets": [{"table": "waybills", "column": "receiver_zip_old"}],
}


def test_health():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_sample_end_to_end():
    resp = client.post("/analyze", json=SAMPLE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    stg = body["models"]["stg_waybill"]
    assert stg["dependent_output_columns"] == [{
        "output_column": "zip",
        "reference_chains": [[
            "stg_waybill.zip", "stg_waybill.cleaned.zip",
            "waybills.receiver_zip_old",
        ]],
    }]
    assert stg["filter_conditions"][0]["type"] == "WHERE"
    fact = body["models"]["dwd_delivery_fact"]
    assert {e["output_column"] for e in fact["dependent_output_columns"]} == {"receiver_zip"}
    assert {c["type"] for c in fact["filter_conditions"]} == {"WHERE", "JOIN ON"}
    # chains cross the model boundary (and retain the CTE hop from the star)
    chain = fact["dependent_output_columns"][0]["reference_chains"][0]
    assert chain == ["dwd_delivery_fact.receiver_zip", "stg_waybill.zip",
                     "stg_waybill.cleaned.zip", "waybills.receiver_zip_old"]


def test_ambiguous_column_returns_400_with_model():
    payload = {
        "sources": SAMPLE["sources"],
        "models": [{
            "name": "bad_model",
            "sql": ("SELECT receiver_zip_old FROM waybills a"
                    " JOIN waybills b ON a.waybill_id = b.waybill_id"),
        }],
        "targets": SAMPLE["targets"],
    }
    resp = client.post("/analyze", json=payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail[0]["model"] == "bad_model"
    assert "歧义" in detail[0]["message"]


def test_unsupported_aggregate_returns_400():
    payload = {
        "sources": SAMPLE["sources"],
        "models": [{"name": "agg", "sql": "SELECT count(*) FROM waybills"}],
        "targets": SAMPLE["targets"],
    }
    resp = client.post("/analyze", json=payload)
    assert resp.status_code == 400
    assert "聚合" in resp.json()["detail"][0]["message"]


def test_unknown_target_returns_400():
    payload = dict(SAMPLE)
    payload["targets"] = [{"table": "waybills", "column": "nope"}]
    resp = client.post("/analyze", json=payload)
    assert resp.status_code == 400


def test_swagger_examples_present():
    schema = client.get("/openapi.json").json()
    examples = (schema["components"]["schemas"]["AnalyzeRequest"]["examples"])
    assert examples[0]["targets"][0]["column"] == "receiver_zip_old"
    # the collision sample ships first so Try-it-out shows the dotted-name case
    assert {"carrier.postcode", "carrier"} <= {
        m["name"] for m in examples[0]["models"]}
    assert "/analyze" in schema["paths"]


def test_swagger_request_body_examples_are_submittable():
    schema = client.get("/openapi.json").json()
    media = schema["paths"]["/analyze"]["post"]["requestBody"]["content"][
        "application/json"]
    body_examples = media["examples"]
    assert "dotted_scope_collision" in body_examples
    # every Swagger sample must actually analyze successfully via Try-it-out
    for name, example in body_examples.items():
        resp = client.post("/analyze", json=example["value"])
        assert resp.status_code == 200, (name, resp.text)


def test_dot_collision_example_end_to_end():
    from app.main import QUOTED_SCOPE_COLLISION_EXAMPLE

    resp = client.post("/analyze", json=QUOTED_SCOPE_COLLISION_EXAMPLE)
    assert resp.status_code == 200, resp.text
    body = resp.json()["models"]
    # model name "carrier.postcode" is quoted as one identifier
    assert body["carrier.postcode"]["dependent_output_columns"] == [{
        "output_column": "postcode_zip",
        "reference_chains": [[
            '"carrier.postcode".postcode_zip', "waybills.receiver_zip_old"]],
    }]
    # CTE scope carrier.postcode belongs to model carrier, unquoted
    assert body["carrier"]["dependent_output_columns"] == [{
        "output_column": "zip",
        "reference_chains": [[
            "carrier.zip", "carrier.postcode.zip", "waybills.receiver_zip_old"]],
    }]
    assert body["carrier"]["filter_conditions"][0]["scope"] == "carrier.postcode"
    # the downstream model keeps both distinct paths
    fact = body["dwd_delivery_fact"]["dependent_output_columns"]
    chains = {tuple(c) for entry in fact for c in entry["reference_chains"]}
    assert (
        "dwd_delivery_fact.carrier_zip", "carrier.zip", "carrier.postcode.zip",
        "waybills.receiver_zip_old") in chains
    assert (
        "dwd_delivery_fact.postcode_zip", '"carrier.postcode".postcode_zip',
        "waybills.receiver_zip_old") in chains


def test_plain_names_response_structure_unchanged():
    resp = client.post("/analyze", json=SAMPLE)
    assert resp.status_code == 200
    assert resp.json()["models"]["stg_waybill"]["dependent_output_columns"][0][
        "reference_chains"][0] == [
        "stg_waybill.zip", "stg_waybill.cleaned.zip",
        "waybills.receiver_zip_old"]
