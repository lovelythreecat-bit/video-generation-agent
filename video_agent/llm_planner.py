from __future__ import annotations

import json
import re

from video_agent.config import ASPECT_RATIOS
from video_agent.errors import VideoAgentError
from video_agent.models import PlanDocument, PlanPatch, SharedVisual, ShotPlan
from video_agent.openai_chat import complete
from video_agent.ports import PlanRequest, PlanResult, ReviseResult

_SYSTEM = """你是电商短视频分镜策划。只根据给出的产品文字写方案。输入里没有的价格、功效、规格、认证和绝对化承诺不要写。
你看不到产品图片。只输出一个 JSON 对象，不要 Markdown。
可以只返回 {"questions":["还不能写方案的具体问题"]}。
否则 questions 必须为 null，并给出这些字段：
creative, audience, selling_points,
shared_visual 对象（product_reference, scene, lighting, palette, style，可选 character, scene_change）,
shots 数组。每个镜头含 content, action, shot_size, camera, scene_id, seconds, subtitle_text, voiceover_text。
另有 music_direction, language。
seconds 是正整数。所有镜头的 seconds 相加必须等于 duration_seconds。单个镜头不能超过 max_segment_seconds。
字幕关闭时 subtitle_text 为空。配音关闭时 voiceover_text 为空。音乐关闭时 music_direction 为空。
默认整条片子同一个 scene_id。只有用户明确要求换场景时才换。"""


class OpenAIChatPlanner:
    def __init__(self, *, base_url: str, api_key: str, model: str, transport=None) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.transport = transport

    def capabilities_note(self) -> str:
        return "文本大模型按视频接口的单段上限拆镜头，不直接观看产品图片"

    def plan(self, request: PlanRequest) -> PlanResult:
        return self._validated_ask(
            {"task": "plan", "brief": _brief(request)},
            lambda raw: interpret_plan(raw, request, self.capabilities_note()),
        )

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult:
        return self._validated_ask(
            {
                "task": "revise",
                "instruction": instruction,
                "current_plan": current.model_dump(),
                "brief": _brief(request),
                "allowed_durations": list(request.duration_tiers),
                "allowed_aspect_ratios": list(ASPECT_RATIOS),
            },
            lambda raw: interpret_revision(raw, current, request),
        )

    def _validated_ask(self, payload, interpret):
        raw = self._ask(_SYSTEM, payload)
        try:
            return interpret(raw)
        except VideoAgentError as error:
            if error.code != "PROVIDER_REJECTED":
                raise
            # Repair model output once, preserving the original brief/revision.
            # HTTP/auth failures are outside this block and never replayed here.
            repair = {
                "previous_output": raw,
                "validation_error": error.message,
                "instruction": (
                    "修正上次输出并返回完整 JSON。遵守 brief 的单镜头上限；"
                    "所有镜头秒数之和必须等于方案目标时长。保留商品事实和用户要求。"
                    + ("初次规划严格遵守 brief 的总时长、比例和开关。"
                       if payload["task"] == "plan" else
                       "修订时按原始 instruction 保留用户要求的时长、比例和开关变更，"
                       "不要退回 brief 的旧选项；时长须在 allowed_durations 内，"
                       "比例须在 allowed_aspect_ratios 内。")
                ),
            }
        corrected = self._ask(_SYSTEM, {**payload, "repair": repair})
        return interpret(corrected)

    def _ask(self, system: str, payload: dict) -> str:
        return complete(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            timeout=120,
            transport=self.transport,
        )


def interpret_plan(raw: str, request: PlanRequest, note: str) -> PlanResult:
    payload = _json_object(raw)
    questions = _questions(payload)
    if questions is not None:
        return PlanResult(questions=questions)
    visual = _visual(payload)
    shots = _shots(payload.get("shots"), request.duration_seconds, request.video_capabilities.max_segment_seconds)
    _require_copy(request, shots, _text(payload, "music_direction"))
    native = request.aspect_ratio in request.video_capabilities.native_aspects
    language = request.audio.language if _filled(request.audio.language) else _text(payload, "language") or None
    document = PlanDocument(
        creative=_required_text(payload, "creative", "创意"),
        audience=_required_text(payload, "audience", "受众"),
        selling_points=_points(payload, request),
        shared_visual=visual,
        shots=shots,
        music_direction=_text(payload, "music_direction") if request.music else "",
        music_asset_id=request.audio.music_asset_id if request.music else None,
        duration_seconds=request.duration_seconds,
        aspect_ratio=request.aspect_ratio,
        fit="native" if native else "pad",
        subtitles=request.subtitles,
        voiceover=request.voiceover,
        music=request.music,
        first_frame_source="selected" if request.first_frame_asset_id else "generate",
        first_frame_asset_id=request.first_frame_asset_id,
        model_capabilities=note,
        language=language,
        voice_preference=request.audio.voice_preference,
    )
    return PlanResult(document=document)


def interpret_revision(raw: str, current: PlanDocument, request: PlanRequest) -> ReviseResult:
    payload = _json_object(raw)
    questions = _questions(payload)
    if questions is not None:
        return ReviseResult(questions=questions)
    duration = _optional_int(payload.get("duration_seconds"), current.duration_seconds)
    if duration not in request.duration_tiers:
        tiers = "、".join(str(item) for item in request.duration_tiers)
        return ReviseResult(questions=[f"时长只能选择 {tiers} 秒"])
    aspect = _text(payload, "aspect_ratio") or current.aspect_ratio
    if aspect not in ASPECT_RATIOS:
        return ReviseResult(questions=["画面比例不在预设里"])
    subtitles = _optional_bool(payload.get("subtitles"), current.subtitles)
    voiceover = _optional_bool(payload.get("voiceover"), current.voiceover)
    music = _optional_bool(payload.get("music"), current.music)
    draft = request.model_copy(
        update={
            "duration_seconds": duration,
            "aspect_ratio": aspect,
            "subtitles": subtitles,
            "voiceover": voiceover,
            "music": music,
        }
    )
    visual = _visual(payload)
    shots = _shots(payload.get("shots"), duration, request.video_capabilities.max_segment_seconds)
    music_direction = _text(payload, "music_direction") if music else ""
    _require_copy(draft, shots, music_direction)
    native = aspect in request.video_capabilities.native_aspects
    fit = "native" if native else "pad"
    language = _text(payload, "language") or current.language
    patch = PlanPatch(
        duration_seconds=None if duration == current.duration_seconds else duration,
        aspect_ratio=None if aspect == current.aspect_ratio else aspect,
        fit=None if fit == current.fit else fit,
        subtitles=None if subtitles == current.subtitles else subtitles,
        voiceover=None if voiceover == current.voiceover else voiceover,
        music=None if music == current.music else music,
        language=None if language == current.language else language,
        music_direction=None if music_direction == current.music_direction else music_direction,
        shared_visual=visual,
        shots=shots,
    )
    return ReviseResult(patch=patch)


def _brief(request: PlanRequest) -> dict:
    return {
        "product_info": request.product_info.model_dump(),
        "user_prompt": request.user_prompt,
        "image_count": len(request.images),
        "duration_seconds": request.duration_seconds,
        "duration_tiers": list(request.duration_tiers),
        "aspect_ratio": request.aspect_ratio,
        "subtitles": request.subtitles,
        "voiceover": request.voiceover,
        "music": request.music,
        "language": request.audio.language,
        "voice_preference": request.audio.voice_preference,
        "music_direction_hint": request.audio.music_direction,
        "max_segment_seconds": request.video_capabilities.max_segment_seconds,
        "has_selected_first_frame": request.first_frame_asset_id is not None,
    }


def _json_object(raw: str) -> dict:
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced is not None:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise VideoAgentError("PROVIDER_REJECTED", "文本大模型没有返回 JSON", retryable=True)
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise VideoAgentError("PROVIDER_REJECTED", "文本大模型返回的 JSON 无法解析", retryable=True) from exc
    if not isinstance(value, dict):
        raise VideoAgentError("PROVIDER_REJECTED", "文本大模型没有返回 JSON 对象", retryable=True)
    return value


def _questions(payload: dict) -> list[str] | None:
    raw = payload.get("questions")
    if not isinstance(raw, list):
        return None
    questions = [str(item).strip() for item in raw if str(item).strip()]
    return questions or None


def _visual(payload: dict) -> SharedVisual:
    raw = payload.get("shared_visual")
    if not isinstance(raw, dict):
        raise VideoAgentError("PROVIDER_REJECTED", "文本大模型没有给出共享画面", retryable=True)
    def take(name: str, label: str) -> str:
        value = raw.get(name)
        if not isinstance(value, str) or value.strip() == "":
            raise VideoAgentError("PROVIDER_REJECTED", f"文本大模型没有给出{label}", retryable=True)
        return value.strip()

    character = raw.get("character")
    scene_change = raw.get("scene_change")
    return SharedVisual(
        product_reference=take("product_reference", "产品外观"),
        scene=take("scene", "场景"),
        lighting=take("lighting", "光线"),
        palette=take("palette", "色彩"),
        style=take("style", "风格"),
        character=character.strip() if isinstance(character, str) and character.strip() else None,
        scene_change=scene_change.strip() if isinstance(scene_change, str) and scene_change.strip() else None,
    )


def _shots(raw: object, duration_seconds: int, max_segment_seconds: int) -> list[ShotPlan]:
    if not isinstance(raw, list) or len(raw) == 0:
        raise VideoAgentError("PROVIDER_REJECTED", "文本大模型没有给出镜头", retryable=True)
    shots: list[ShotPlan] = []
    cursor = 0
    total = 0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise VideoAgentError("PROVIDER_REJECTED", "文本大模型的镜头格式不对", retryable=True)
        seconds = _whole_seconds(item.get("seconds"))
        if seconds < 1:
            raise VideoAgentError("PROVIDER_REJECTED", "每个镜头至少 1 秒", retryable=True)
        if seconds > max_segment_seconds:
            raise VideoAgentError(
                "PROVIDER_REJECTED",
                f"单个镜头不能超过 {max_segment_seconds} 秒，模型给了 {seconds} 秒",
                retryable=True,
            )
        total += seconds
        frames = seconds * 30
        shots.append(
            ShotPlan(
                shot_id=f"s{index}",
                content=_item_text(item, "content", "镜头内容"),
                action=_item_text(item, "action", "镜头动作"),
                shot_size=_item_text(item, "shot_size", "景别"),
                camera=_item_text(item, "camera", "运镜"),
                scene_id=_item_text(item, "scene_id", "场景编号", whole_number=True),
                start_frame=cursor,
                end_frame=cursor + frames,
                subtitle_text=_item_text(item, "subtitle_text", "字幕", allow_empty=True),
                voiceover_text=_item_text(item, "voiceover_text", "配音", allow_empty=True),
            )
        )
        cursor += frames
    if total != duration_seconds:
        raise VideoAgentError(
            "PROVIDER_REJECTED",
            f"镜头秒数加起来是 {total}，这一条要 {duration_seconds} 秒",
            retryable=True,
        )
    return shots


def _require_copy(request: PlanRequest, shots: list[ShotPlan], music_direction: str) -> None:
    if request.subtitles and any(shot.subtitle_text.strip() == "" for shot in shots):
        raise VideoAgentError("PROVIDER_REJECTED", "字幕开着，但有镜头没有字幕", retryable=True)
    if request.voiceover and any(shot.voiceover_text.strip() == "" for shot in shots):
        raise VideoAgentError("PROVIDER_REJECTED", "配音开着，但有镜头没有配音", retryable=True)
    if request.music and music_direction.strip() == "" and not request.audio.music_asset_id:
        raise VideoAgentError("PROVIDER_REJECTED", "音乐开着，但没有音乐方向", retryable=True)


def _points(payload: dict, request: PlanRequest) -> list[str]:
    raw = payload.get("selling_points")
    if isinstance(raw, list):
        points = [str(item).strip() for item in raw if str(item).strip()]
        if points:
            return points
    return list(request.product_info.selling_points)


def _required_text(payload: dict, key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise VideoAgentError("PROVIDER_REJECTED", f"文本大模型没有给出{label}", retryable=True)
    return value.strip()


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _item_text(item: dict, key: str, label: str, *, allow_empty: bool = False, whole_number: bool = False) -> str:
    value = item.get(key, "")
    if value is None:
        value = ""
    if whole_number:
        number = _whole_number_text(value)
        if number is not None:
            value = number
    if not isinstance(value, str):
        raise VideoAgentError("PROVIDER_REJECTED", f"文本大模型的{label}格式不对", retryable=True)
    if value.strip() == "" and not allow_empty:
        raise VideoAgentError("PROVIDER_REJECTED", f"文本大模型没有给出{label}", retryable=True)
    return value.strip()


def _whole_number_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return str(int(value))


def _whole_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoAgentError("PROVIDER_REJECTED", "镜头秒数必须是整数", retryable=True)
    if isinstance(value, float) and not value.is_integer():
        raise VideoAgentError("PROVIDER_REJECTED", "镜头秒数必须是整数", retryable=True)
    return int(value)


def _optional_int(value: object, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    return _whole_seconds(value)


def _optional_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _filled(value: str | None) -> bool:
    return isinstance(value, str) and value.strip() != ""
