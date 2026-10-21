from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from video_agent.models import AudioOptions, CreateTaskRequest, InputPatch, PlanChange, PlanPatch, ProductInfo, RegenerateRequest, SharedVisual, ShotPlan, ShotTextPatch, VideoOptions


class StrictProductInfo(ProductInfo):
    model_config = ConfigDict(extra="forbid")


class StrictVideoOptions(VideoOptions):
    model_config = ConfigDict(extra="forbid")


class StrictAudioOptions(AudioOptions):
    model_config = ConfigDict(extra="forbid")


class StrictCreateTaskRequest(CreateTaskRequest):
    model_config = ConfigDict(extra="forbid")
    product_info: StrictProductInfo = Field(default_factory=StrictProductInfo, description="商品名称、类别与卖点。")
    video_options: StrictVideoOptions = Field(default_factory=StrictVideoOptions, description="时长、比例及模型画面文字/音频开关。")
    audio_options: StrictAudioOptions = Field(default_factory=StrictAudioOptions, description="语言、声音与音乐偏好。")


class StrictInputPatch(InputPatch):
    model_config = ConfigDict(extra="forbid")
    product_info: StrictProductInfo | None = None
    video_options: StrictVideoOptions | None = None
    audio_options: StrictAudioOptions | None = None


class StrictSharedVisual(SharedVisual):
    model_config = ConfigDict(extra="forbid")


class StrictShotPlan(ShotPlan):
    model_config = ConfigDict(extra="forbid")


class StrictShotTextPatch(ShotTextPatch):
    model_config = ConfigDict(extra="forbid")


class StrictPlanPatch(PlanPatch):
    model_config = ConfigDict(extra="forbid")
    shared_visual: StrictSharedVisual | None = None
    shots: list[StrictShotPlan] | None = None
    shot_texts: list[StrictShotTextPatch] | None = None


class StrictPlanChange(PlanChange):
    model_config = ConfigDict(extra="forbid")
    patch: StrictPlanPatch | None = None


class StrictRegenerateRequest(RegenerateRequest):
    model_config = ConfigDict(extra="forbid")


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_version: Annotated[int, Field(strict=True, ge=1, description="要确认的当前方案版本；确认后异步生成视频。", examples=[1])]


class PublicSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, description="GET settings 返回的 configuration_revision；过期返回 409。")
    mode: Literal["fake", "real"] | None = Field(default=None, description="fake 使用替身；real 要求有效的真实 planner/video 绑定。")
    duration_tiers: list[int] | None = Field(default=None, description="可选视频时长（秒），每档 1～30，不可重复。")
    default_duration: int | None = Field(default=None, description="默认时长，必须属于 duration_tiers。")
    max_images: int | None = Field(default=None, ge=1, le=12, description="每任务图片数量上限。")
    max_file_bytes: int | None = Field(default=None, ge=1, le=20971520, description="单文件字节上限，最高 20 MiB。")
    video_bitrate: int | None = Field(default=None, ge=1, description="视频目标码率（bit/s）。")
    audio_bitrate: int | None = Field(default=None, ge=1, description="音频目标码率（bit/s）。")
    lease_seconds: int | None = Field(default=None, ge=1, description="单个工作项租约秒数。")
    resolutions: dict[str, tuple[int, int]] | None = Field(default=None, description="比例到宽高像素的完整映射，宽高须为偶数且符合比例。")


class ErrorDetail(BaseModel):
    code: str = Field(description="稳定的机器可读错误码。", examples=["STATE_CONFLICT"])
    message: str = Field(description="脱敏中文错误说明。")
    retryable: bool = Field(description="相同操作是否可稍后重试。")
    request_id: str = Field(description="服务生成的请求跟踪标识。")
    task_id: str | None = None
    generation_id: str | None = None
    plan_version: int | None = None
    fields: list[str] | None = Field(default=None, description="存在错误的字段名；不包含输入值。")
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthView(BaseModel):
    status: str = Field(description="live 表示进程存活，ready 表示执行依赖全部就绪。")


class AdapterTypeView(BaseModel):
    adapter: str
    roles: list[str]
    required_fields: list[str] = Field(default_factory=list)
    secret_fields: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] | list[Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict, description="适配器公开能力元数据，不包含当前配置。")


class CapabilitiesView(BaseModel):
    aspect_ratios: list[str]
    duration_tiers: list[int]
    default_duration: int
    max_images: int
    max_file_bytes: int
    image_media_types: list[str]
    audio_media_types: list[str]
    adapters: list[AdapterTypeView]


ERROR_RESPONSES = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in {
        401: "缺少或错误的 Bearer Token。",
        403: "业务 Token 无管理权限。",
        404: "资源不存在或文件已缺失。",
        409: "状态、配置版本或幂等请求冲突。",
        413: "上传实际字节数超过单文件上限。",
        422: "参数或字段校验失败。",
        500: "内部错误；响应不包含堆栈或服务路径。",
        503: "数据库、Worker 或执行依赖暂不可用。",
    }.items()
}


FIELD_DESCRIPTIONS = {
    "visual_check_enabled": "新流程已移除视觉检查，新成品为 false；历史成品按生成记录返回，null 表示无法确认。不等于检查通过。",
    "task_id": "任务唯一标识。", "status": "资源当前状态；异步操作需要轮询。", "stage": "执行阶段。", "stage_detail": "当前阶段的脱敏说明。", "failure_scope": "失败范围：规划或视频生成。", "failure": "失败信息，供应商原始消息不对外返回。", "questions": "规划需要补充的信息。", "external_user_id": "调用方用户标识，仅用于精确筛选，不构成权限凭证。", "external_request_id": "调用方请求标识，精确且区分大小写。", "head_plan_version": "当前方案头版本，尚未规划时为空。", "confirmed_plan_version": "最近确认的方案版本。", "generation_id": "视频生成执行标识。", "completed_shots": "已完成的镜头数。", "total_shots": "本次执行的镜头总数。", "product_name": "商品名称摘要。", "product_info": "商品资料。", "user_prompt": "调用方的自由文本创作要求。", "video_options": "视频时长、比例与开关。", "audio_options": "语言、声音与音乐偏好。", "image_asset_ids": "关联的图片素材 ID。", "images": "先上传得到的图片素材 ID；不得传服务端路径。", "first_frame_asset_id": "首帧图片 ID，必须属于本任务图片。", "updated_at": "最后更新时间，UTC ISO 8601。", "created_at": "创建时间，UTC ISO 8601。", "name": "显示名称；模型配置允许重名。", "category": "商品类别。", "selling_points": "商品卖点列表。", "specs": "商品规格说明。", "extra": "调用方提供的其他商品信息。", "duration_seconds": "视频时长（秒），须属于当前时长档。", "aspect_ratio": "画面比例，如 9:16；可选项见 capabilities。", "subtitles": "是否输出字幕。", "voiceover": "请求视频模型自带音频，不再独立合成配音；模型无音轨时允许静音导出。", "music": "请求视频模型自带音频，不再后期配乐；与 voiceover 同时关闭时移除音轨。", "language": "文案与声音语言偏好。", "voice_preference": "声音风格偏好。", "music_direction": "音乐风格要求。", "music_asset_id": "兼容历史调用的音频素材 ID；当前导出流程不再使用它进行配乐。", "clear_first_frame": "显式移除已选择首帧。", "clear_music_asset": "显式移除音乐素材绑定。", "creative": "方案创意。", "audience": "目标受众。", "shared_visual": "镜头共享的视觉要求。", "shots": "按帧区间排列的完整镜头方案。", "shot_id": "方案内镜头标识。", "shot_ids": "选中的镜头标识。", "shot_texts": "只修改指定镜头文案的补丁。", "content": "镜头内容。", "action": "主体动作。", "shot_size": "景别。", "camera": "运镜方式。", "scene_id": "场景标识。", "start_frame": "镜头起始帧，30 fps。", "end_frame": "镜头结束帧，不包含该帧，30 fps。", "subtitle_text": "字幕文案。", "voiceover_text": "配音文案。", "product_reference": "商品外观参考。", "scene": "场景描述。", "lighting": "光线要求。", "palette": "色调要求。", "style": "视觉风格。", "character": "人物要求。", "scene_change": "场景切换要求。", "fit": "比例适配方式：native 原生、pad 留边、crop 裁切。", "first_frame_source": "首帧来源：selected 已选素材，generate 模型生成。", "model_capabilities": "方案采用的模型能力说明。", "instruction": "自然语言修改要求；排队处理后产生新版本。", "patch": "结构化方案修改；校验通过后立即产生新版本。", "plan_version": "方案版本号。", "is_head": "是否为当前方案头。", "confirmed": "是否已确认。", "abandoned": "是否已放弃。", "document": "完整方案文档。", "kind": "操作种类；重生成使用 full 或 shots。", "base_artifact_id": "局部重生成所依据的成品 ID。", "artifact_id": "成品唯一标识。", "version": "资源版本号；更新时用于乐观并发检查。", "width": "输出宽度（像素）。", "height": "输出高度（像素）。", "quality_status": "视觉检查状态：passed、flagged 或 not_assessed。", "quality": "脱敏的视觉检查结果。", "limit": "本页最大数量，默认 20、最高 100。", "offset": "跳过记录数，最小为 0。", "total": "符合筛选条件的记录总数。", "items": "本页记录。", "asset_id": "素材唯一标识。", "filename": "经过安全处理的原始文件名。", "media_type": "根据内容识别的 MIME 类型。", "byte_size": "文件实际字节数。", "profile_id": "模型配置唯一标识。", "role": "适配器角色：planner、video、voice、music 或 visual。", "adapter": "注册的适配器名称。", "base_url": "供应商接口地址，不允许内嵌秘密。", "model": "供应商模型标识。", "parameters": "适配器非敏感参数；敏感凭据必须使用 secrets。", "secrets": "只写敏感凭据；省略或空字符串表示保留。", "enabled": "配置是否启用；绑定中的配置不可停用。", "expected_version": "必须等于刚读取的 version 或 configuration_revision；过期返回 409。", "clear_secrets": "显式清除的秘密字段名，与同名新秘密不可同时提供。", "has_api_key": "是否已保存 api_key；不会回显其值。", "secret_presence": "按秘密字段名给出的存在标记。", "configuration_revision": "整组配置单调递增修订，用于 settings/bindings 的 expected_version。", "planner": "规划角色绑定。", "video": "视频生成角色绑定。", "voice": "配音角色绑定。", "visual": "视觉检查角色绑定。", "mode": "fake 替身模式或 real 真实模式。", "duration_tiers": "可选时长档（秒），每档 1～30。", "default_duration": "未显式指定时使用的时长档。", "max_images": "每任务图片数量上限，最高 12。", "max_file_bytes": "上传实际单文件字节数上限，最高 20 MiB。", "video_bitrate": "视频目标码率（bit/s）。", "audio_bitrate": "音频目标码率（bit/s）。", "lease_seconds": "单个工作项租约秒数。", "resolutions": "比例到宽高像素的完整映射，尺寸为偶数且符合比例。", "aspect_ratios": "支持的比例列表。", "image_media_types": "支持的图片 MIME 类型。", "audio_media_types": "支持的音频 MIME 类型。", "adapters": "注册的公开适配器能力元数据。", "roles": "该适配器可用于的角色。", "required_fields": "配置必填字段名。", "secret_fields": "需要保密的字段名。", "capabilities": "适配器公开能力，不包含用户配置或凭据。", "code": "稳定的机器可读错误码。", "message": "脱敏中文说明。", "retryable": "此操作是否可稍后重试。", "request_id": "服务生成的请求跟踪标识。", "fields": "校验失败的字段名；不含输入值。", "details": "可选的脱敏诊断信息。", "error": "统一错误对象。",
}


def add_chinese_field_documentation(document: dict) -> dict:
    """Describe reused core models without changing their business behavior."""
    for schema in document.get("components", {}).get("schemas", {}).values():
        for name, field in schema.get("properties", {}).items():
            if not field.get("description") and name in FIELD_DESCRIPTIONS:
                field["description"] = FIELD_DESCRIPTIONS[name]
    return document
