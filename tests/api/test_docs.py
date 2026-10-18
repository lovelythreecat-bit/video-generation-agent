import re


def test_online_docs_and_local_assets(api):
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = api.client.get(path)
        assert response.status_code == 200, response.text
        if path != "/openapi.json":
            assert "https://" not in response.text
            assert "http://" not in response.text
            for asset in re.findall(r'(?:src|href)="(/static/docs/[^\"]+)"', response.text):
                downloaded = api.client.get(asset)
                assert downloaded.status_code == 200
                assert len(downloaded.content) > 10
    assert len(api.client.get("/static/docs/swagger-ui-bundle.js").content) > 100000
    assert len(api.client.get("/static/docs/redoc.standalone.js").content) > 100000
    assert '"persistAuthorization": false' in api.client.get("/docs").text


def test_openapi_covers_routes_and_auth(api):
    schema = api.client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    ids = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            ids.append(operation["operationId"])
            assert operation["summary"]
            assert any(status.startswith("2") for status in operation["responses"])
            if path.startswith("/api/"):
                assert operation["security"] == [{"BearerAuth": []}]
                assert "422" in operation["responses"]
                assert operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")
    assert len(ids) == len(set(ids))
    assert len(ids) == 29
    assert "multipart/form-data" in schema["paths"]["/api/v1/assets"]["post"]["requestBody"]["content"]
    assert "Idempotency-Key" in [p["name"] for p in schema["paths"]["/api/v1/tasks"]["post"]["parameters"]]


def test_docs_examples_contain_no_credentials(api):
    text = api.client.get("/openapi.json").text
    for secret in (api.runtime.secret_key, api.runtime.business_token, api.runtime.admin_token):
        assert secret not in text
    assert "copied_config_json" not in text
