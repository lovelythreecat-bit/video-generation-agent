from __future__ import annotations

import re

from video_agent.models import PlanDocument, PlanPatch, ProductInfo, SharedVisual, ShotPlan
from video_agent.ports import PlanRequest, PlanResult, ReviseResult

_RATIOS = ("16:9", "9:16", "4:5", "3:4", "4:3", "3:2", "2:3", "1:1")
_SWITCHES = (
    ("关闭字幕", "subtitles", False),
    ("开启字幕", "subtitles", True),
    ("关闭配音", "voiceover", False),
    ("开启配音", "voiceover", True),
    ("关闭音乐", "music", False),
    ("开启音乐", "music", True),
)


class FakePlanner:
    def capabilities_note(self) -> str:
        return "替身规划器只把 9:16 当作原生比例，其他比例使用 pad"

    def plan(self, request: PlanRequest) -> PlanResult:
        if request.subtitles or request.voiceover:
            conflict = _language_conflict(request.product_info, request.user_prompt)
            if conflict is not None:
                return PlanResult(questions=[conflict])
        product = request.product_info
        subtitle = " ".join(product.selling_points) if product.selling_points else (product.name or "产品")
        voice = product.name or subtitle
        total = request.duration_seconds * 30
        native = request.aspect_ratio in request.video_capabilities.native_aspects
        music_on = request.music
        document = PlanDocument(
            creative=f"展示{product.name or '产品'}",
            audience="商品观众",
            selling_points=list(product.selling_points),
            shared_visual=SharedVisual(
                product_reference=f"产品图片 {request.images[0]}" if request.images else "产品图片",
                scene="同一简洁背景",
                lighting="柔和均匀光线",
                palette="跟随产品图片",
                style="写实",
            ),
            shots=[
                ShotPlan(
                    shot_id="s1",
                    content=product.name or "产品",
                    action="缓慢旋转展示",
                    shot_size="中景",
                    camera="固定镜头",
                    scene_id="scene-1",
                    start_frame=0,
                    end_frame=total,
                    subtitle_text=subtitle if request.subtitles else "",
                    voiceover_text=voice if request.voiceover else "",
                )
            ],
            music_direction=(request.audio.music_direction or "平稳背景") if music_on else "",
            music_asset_id=request.audio.music_asset_id if music_on else None,
            duration_seconds=request.duration_seconds,
            aspect_ratio=request.aspect_ratio,
            fit="native" if native else "pad",
            subtitles=request.subtitles,
            voiceover=request.voiceover,
            music=music_on,
            first_frame_source="selected" if request.first_frame_asset_id else "generate",
            first_frame_asset_id=request.first_frame_asset_id,
            model_capabilities=_capabilities(self.capabilities_note(), request),
            language=_language_of(_product_text(product)) or _language_of(request.user_prompt),
            voice_preference=request.audio.voice_preference,
        )
        return PlanResult(document=document)

    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult:
        del current
        text = instruction.strip()
        patch = PlanPatch()
        used = False
        seconds = re.findall(r"(\d+)\s*秒", text)
        if seconds:
            value = int(seconds[0])
            if value not in request.duration_tiers:
                tiers = "、".join(str(item) for item in request.duration_tiers)
                return ReviseResult(questions=[f"时长只能选择 {tiers} 秒"])
            patch.duration_seconds = value
            used = True
        for ratio in _RATIOS:
            if ratio in text:
                patch.aspect_ratio = ratio
                patch.fit = "native" if ratio in request.video_capabilities.native_aspects else "pad"
                used = True
                break
        for phrase, field, value in _SWITCHES:
            if phrase in text:
                setattr(patch, field, value)
                used = True
        if not used:
            return ReviseResult()
        return ReviseResult(patch=patch)


def _product_text(info: ProductInfo) -> str:
    parts = [info.name or "", info.category or "", info.specs or "", *info.selling_points, *info.extra.values()]
    return "\n".join(parts)


def _language_of(text: str) -> str | None:
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if han == 0 and latin == 0:
        return None
    if han == latin:
        return None
    return "zh" if han > latin else "en"


def _language_conflict(info: ProductInfo, prompt: str) -> str | None:
    product = _language_of(_product_text(info))
    user = _language_of(prompt)
    if product is None or user is None or product == user:
        return None
    names = {"zh": "中文", "en": "英文"}
    return f"产品资料主语言是{names[product]}，用户要求主语言是{names[user]}，请明确配音和字幕语言"


def _capabilities(note: str, request: PlanRequest) -> str:
    foreign = []
    for match in re.findall(r"(\d+)\s*秒", request.user_prompt):
        value = int(match)
        if value not in request.duration_tiers and value not in foreign:
            foreign.append(value)
    if not foreign:
        return note
    tiers = "、".join(str(item) for item in request.duration_tiers)
    found = "、".join(str(item) for item in foreign)
    return f"{note}。当前时长档位为 {tiers} 秒，描述中的 {found} 秒不会作为目标时长"
