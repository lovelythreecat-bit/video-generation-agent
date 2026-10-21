import json

import pytest

from .conftest import create, plan


@pytest.mark.parametrize(
    "mode,visual,expected",
    [("fake", "none", False), ("real", "none", False), ("real", "openai_vision", True)],
)
@pytest.mark.parametrize("direct_export", [False, True])
def test_artifact_review_setting_comes_from_generation_snapshot(
    api, auth, png, mode, visual, expected, direct_export
):
    task = create(api, auth, png)
    planned = plan(api, task["task_id"])
    generation = api.agent.confirm(
        task["task_id"], planned.head_plan_version
    ).generation_id
    config = api.settings.read_effective_config().model_copy(deep=True)
    config.mode = mode
    config.adapters["visual"].adapter = visual
    snapshot = api.settings.codec.encode(config)
    with api.agent.db.transaction() as conn:
        conn.execute(
            "UPDATE generations SET copied_config_json=:snapshot WHERE generation_id=:id",
            {"snapshot": json.dumps(snapshot), "id": generation},
        )
        conn.execute(
            "INSERT INTO artifacts (artifact_id,task_id,generation_id,plan_version,version,duration_seconds,aspect_ratio,width,height,relative_path,quality_status,quality_json,created_at) VALUES (:artifact,:task,:generation,:plan,1,10,:ratio,180,320,:path,:quality,:body,:now)",
            {
                "artifact": "test-artifact",
                "task": task["task_id"],
                "generation": generation,
                "plan": planned.head_plan_version,
                "ratio": "9:16",
                "path": "unused.mp4",
                "quality": "not_assessed",
                "body": json.dumps({"visual_check_enabled": False}) if direct_export else "{}",
                "now": api.agent.clock.now().isoformat(),
            },
        )
    response = api.client.get(
        "/api/v1/tasks/" + task["task_id"] + "/artifacts", headers=auth
    )
    assert response.status_code == 200
    artifact = response.json()[0]
    assert artifact["visual_check_enabled"] is (False if direct_export else expected)
    assert artifact["quality_status"] == "not_assessed"
    assert "encrypted_secrets" not in response.text


def test_artifact_review_legacy_unknown_snapshot_is_not_a_disabled_check(
    api, auth, png
):
    task = create(api, auth, png)
    planned = plan(api, task["task_id"])
    generation = api.agent.confirm(
        task["task_id"], planned.head_plan_version
    ).generation_id
    with api.agent.db.transaction() as conn:
        conn.execute(
            "UPDATE generations SET copied_config_json=:snapshot WHERE generation_id=:id",
            {"snapshot": "{}", "id": generation},
        )
        conn.execute(
            "INSERT INTO artifacts (artifact_id,task_id,generation_id,plan_version,version,duration_seconds,aspect_ratio,width,height,relative_path,quality_status,quality_json,created_at) VALUES (:artifact,:task,:generation,:plan,1,10,:ratio,180,320,:path,:quality,:body,:now)",
            {
                "artifact": "test-artifact",
                "task": task["task_id"],
                "generation": generation,
                "plan": planned.head_plan_version,
                "ratio": "9:16",
                "path": "unused.mp4",
                "quality": "not_assessed",
                "body": "{}",
                "now": api.agent.clock.now().isoformat(),
            },
        )
    response = api.client.get(
        "/api/v1/tasks/" + task["task_id"] + "/artifacts", headers=auth
    )
    assert response.status_code == 200
    assert response.json()[0]["visual_check_enabled"] is None
