def profile_body():
    return {"name": "测试规划模型", "role": "planner", "adapter": "openai_chat", "base_url": "https://example.invalid/v1", "model": "example-model", "secrets": {"api_key": "sk-private-model-test"}}


def test_model_profile_crud_masks_secrets(api, admin):
    response = api.client.post("/api/v1/admin/model-profiles", headers=admin, json=profile_body())
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["has_api_key"] is True
    assert "sk-private-model-test" not in response.text
    assert "secrets" not in profile
    base = "/api/v1/admin/model-profiles/" + profile["profile_id"]
    updated = api.client.patch(base, headers=admin, json={"expected_version": profile["version"], "secrets": {"api_key": ""}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["has_api_key"] is True
    assert api.client.patch(base, headers=admin, json={"expected_version": profile["version"], "name": "旧版本"}).status_code == 409
    cleared = api.client.patch(base, headers=admin, json={"expected_version": updated.json()["version"], "clear_secrets": ["api_key"]})
    assert cleared.status_code == 200
    assert cleared.json()["has_api_key"] is False
    assert api.client.get(base, headers=admin).status_code == 200
    assert api.client.delete(base, headers=admin, params={"expected_version": cleared.json()["version"]}).status_code == 204
    assert api.client.get(base, headers=admin).status_code == 404


def test_settings_versions_and_binding_validation(api, admin):
    settings = api.client.get("/api/v1/admin/settings", headers=admin).json()
    revision = settings["configuration_revision"]
    response = api.client.patch("/api/v1/admin/settings", headers=admin, json={"expected_version": revision, "default_duration": 5})
    assert response.status_code == 200, response.text
    assert response.json()["default_duration"] == 5
    assert api.client.patch("/api/v1/admin/settings", headers=admin, json={"expected_version": revision, "default_duration": 10}).status_code == 409
    revision = response.json()["configuration_revision"]
    assert api.client.patch("/api/v1/admin/settings", headers=admin, json={"expected_version": revision, "mode": "real"}).status_code == 422
    profile = api.client.post("/api/v1/admin/model-profiles", headers=admin, json=profile_body()).json()
    revision = api.client.get("/api/v1/admin/adapter-bindings", headers=admin).json()["configuration_revision"]
    binding = api.client.patch("/api/v1/admin/adapter-bindings", headers=admin, json={"expected_version": revision, "planner": {"profile_id": profile["profile_id"]}})
    assert binding.status_code == 200, binding.text
    assert api.client.delete("/api/v1/admin/model-profiles/" + profile["profile_id"], headers=admin, params={"expected_version": profile["version"]}).status_code == 409
    invalid = api.client.patch("/api/v1/admin/adapter-bindings", headers=admin, json={"expected_version": binding.json()["configuration_revision"], "video": {"profile_id": profile["profile_id"]}})
    assert invalid.status_code == 422


def test_profile_create_idempotency(api, admin):
    headers = {**admin, "Idempotency-Key": "profile-create"}
    first = api.client.post("/api/v1/admin/model-profiles", headers=headers, json=profile_body())
    second = api.client.post("/api/v1/admin/model-profiles", headers=headers, json=profile_body())
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert api.client.post("/api/v1/admin/model-profiles", headers=headers, json={**profile_body(), "name": "不同"}).status_code == 409
    other = api.client.post("/api/v1/admin/model-profiles", headers=admin, json=profile_body()).json()
    assert other["profile_id"] != first.json()["profile_id"]
    page = api.client.get("/api/v1/admin/model-profiles?limit=1&offset=1", headers=admin).json()
    assert page["total"] == 2 and len(page["items"]) == 1


def test_disabled_profile_cannot_bind_and_clear_conflicts(api, admin):
    profile = api.client.post("/api/v1/admin/model-profiles", headers=admin, json={**profile_body(), "enabled": False}).json()
    revision = api.client.get("/api/v1/admin/adapter-bindings", headers=admin).json()["configuration_revision"]
    assert api.client.patch("/api/v1/admin/adapter-bindings", headers=admin, json={"expected_version": revision, "planner": {"profile_id": profile["profile_id"]}}).status_code == 409
    response = api.client.patch("/api/v1/admin/model-profiles/" + profile["profile_id"], headers=admin, json={"expected_version": profile["version"], "secrets": {"api_key": "new"}, "clear_secrets": ["api_key"]})
    assert response.status_code == 422


def test_capabilities_and_adapter_metadata(api, auth, admin):
    response = api.client.get("/api/v1/capabilities", headers=auth)
    assert response.status_code == 200, response.text
    assert "9:16" in response.json()["aspect_ratios"]
    assert response.json()["max_file_bytes"] > 0
    metadata = api.client.get("/api/v1/admin/adapter-types", headers=admin)
    assert metadata.status_code == 200
    assert "openai_chat" in [item["adapter"] for item in metadata.json()]
