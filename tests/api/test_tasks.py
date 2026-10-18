from .conftest import create, plan


def test_create_task_returns_201_without_waiting(api, auth, png):
    task = create(api, auth, png)
    assert task["status"] == "pending"
    assert task["head_plan_version"] is None
    with api.agent.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM work_items").fetchone()["n"] == 1


def test_confirm_returns_202_then_query(api, auth, png):
    task = create(api, auth, png)
    planned = plan(api, task["task_id"])
    url = f"/api/v1/tasks/{planned.task_id}/confirm"
    response = api.client.post(url, headers=auth, json={"plan_version": planned.head_plan_version})
    assert response.status_code == 202, response.text
    generation_id = response.json()["generation_id"]
    assert api.client.get(f"/api/v1/generations/{generation_id}", headers=auth).json()["stage"] == "queued"
    assert api.client.post(url, headers=auth, json={"plan_version": planned.head_plan_version}).status_code == 409


def test_task_pagination_and_filters(api, auth):
    for user, request in [("Case", "one"), ("case", "two"), ("Case", "three")]:
        assert api.client.post("/api/v1/tasks", headers=auth, json={"external_user_id": user, "external_request_id": request}).status_code == 201
    body = api.client.get("/api/v1/tasks?limit=1&offset=1&external_user_id=Case", headers=auth).json()
    assert (body["limit"], body["offset"], body["total"]) == (1, 1, 2)
    assert len(body["items"]) == 1
    assert api.client.get("/api/v1/tasks?external_request_id=two", headers=auth).json()["total"] == 1


def test_idempotency_replay_and_conflict(api, auth):
    headers = {**auth, "Idempotency-Key": "http-create"}
    response = api.client.post("/api/v1/tasks", headers=headers, json={"user_prompt": "杯子"})
    assert response.status_code == 201
    assert api.client.post("/api/v1/tasks", headers=headers, json={"user_prompt": "杯子"}).json() == response.json()
    assert api.client.post("/api/v1/tasks", headers=headers, json={"user_prompt": "不同"}).status_code == 409


def test_revise_retry_regenerate_contracts(api, auth, png):
    task = create(api, auth, png)
    planned = plan(api, task["task_id"])
    base = f"/api/v1/tasks/{planned.task_id}"
    assert api.client.get(base + "/plans/1", headers=auth).status_code == 200
    revision = api.client.post(base + "/plan-revisions", headers=auth, json={"patch": {"music": False}})
    assert revision.status_code == 200, revision.text
    assert revision.json()["head_plan_version"] == 2
    # Discard needs a confirmed baseline; an initial draft has none.
    assert api.client.post(base + "/discard-revision", headers=auth).status_code == 409
    assert api.client.post(base + "/retry-planning", headers=auth).status_code == 409
    assert api.client.post(base + "/regenerations", headers=auth, json={"kind": "full"}).status_code == 409
    assert api.client.post("/api/v1/generations/missing/retry", headers=auth).status_code == 404
    assert api.client.get(base + "/artifacts", headers=auth).json() == []


def test_inputs_dynamic_status_and_unknown_ids(api, auth):
    created = api.client.post("/api/v1/tasks", headers=auth, json={}).json()
    response = api.client.patch(f"/api/v1/tasks/{created['task_id']}/inputs", headers=auth, json={"user_prompt": "待补充图片"})
    assert response.status_code == 200
    assert api.client.get("/api/v1/tasks/unknown", headers=auth).status_code == 404
    assert api.client.get("/api/v1/tasks/unknown/artifacts", headers=auth).status_code == 404
