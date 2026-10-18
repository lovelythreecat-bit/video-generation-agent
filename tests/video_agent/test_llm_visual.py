import json

from PIL import Image

from video_agent.llm_visual import OpenAIVisionChecker
from video_agent.ports import CheckFrame


def test_vision_result_parses_flagged(tmp_path):
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (8, 8), "red").save(image)

    def transport(method, url, *, headers, body, timeout):
        assert url.endswith("/chat/completions")
        assert body["model"] == "qwen-vl-max"
        content = body["messages"][1]["content"]
        assert any(part.get("type") == "image_url" for part in content)
        answer = {"status": "flagged", "reason": "杯身颜色变了", "findings": [{"shot_id": "s1", "issue": "颜色"}]}
        return 200, {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}

    checker = OpenAIVisionChecker(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test",
        model="qwen-vl-max",
        transport=transport,
    )
    result = checker.check([CheckFrame(shot_id="s1", position="start", path=image)], "白色杯子")
    assert result.status == "flagged"
    assert result.findings[0]["shot_id"] == "s1"
    assert "颜色" in result.reason
