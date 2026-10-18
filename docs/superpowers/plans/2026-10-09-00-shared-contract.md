# 共享契约

日期：2026-10-09

这是四份实现计划的共同边界。计划只能引用这里的文件路径、类名、方法名、字段名和表名。缺口写进该计划顶部的「契约决议」，用新的内部辅助函数解决，不要改这些公开名字。

规格来源：`docs/superpowers/specs/2026-10-09-ecommerce-video-agent-design.md`

| 计划 | 文件 | 拥有的实现文件 |
| --- | --- | --- |
| 1 核心包 | `docs/superpowers/plans/2026-10-09-01-core.md` | `pyproject.toml`、`.gitignore`、`video_agent/errors.py`、`clock.py`、`config.py`、`models.py`、`frames.py`、`ports.py`、`files.py`、`db.py`、`lease.py`、`idempotency.py`、`state.py`、`service.py`、`worker.py` |
| 2 成片链路 | `docs/superpowers/plans/2026-10-09-02-pipeline.md` | `video_agent/factory.py`、`fake_planner.py`、`fake_video.py`、`fake_voice.py`、`fake_music.py`、`visual.py`、`media.py`、`pipeline.py` |
| 3 桌面测试台 | `docs/superpowers/plans/2026-10-09-03-desktop.md` | `desktop_app/` |
| 4 重生成与恢复 | `docs/superpowers/plans/2026-10-09-04-recovery.md` | 在计划 2 的链路上补重生成、重试续跑、退避、租约和不确定提交。可以修改 `pipeline.py`、`media.py`、`worker.py`、`lease.py` |

计划 1 的 `tick` 遇到 `kind=generation` 时调用 `video_agent.pipeline.advance_generation`。计划 1 不实现该函数，也不在测试里对生成工作调用 `tick`。计划 2 提供该函数。

真实供应商报文不在任何计划里。`factory.py` 的 `REAL_ADAPTERS: dict[str, object] = {}`。真实模式要求规划和视频适配器已注册且不是 `fake` 或 `none`。

## 每份计划必须原样放进 Global Constraints 的句子

- Python 3.11+。运行时依赖只有 `pydantic>=2` 和 `pillow>=10`。测试依赖 `pytest`。SQLite、Tkinter、哈希和路径使用标准库。
- 可执行文件是本机 PATH 上的 `ffmpeg` 和 `ffprobe`。帧率 30。成片是 H.264、yuv420p 的 MP4。有音轨时是 AAC、48 kHz、立体声。
- 默认视频码率 8_000_000 bps，默认音频码率 192_000 bps。
- 时长档默认 `[5, 10, 15, 30]`，默认选中 10。每一档是 1 到 30 的整数，含 30。档位不可重复，列表不可空，默认档必须属于档位。
- 画面比例只有 `9:16`、`16:9`、`1:1`、`4:5`、`3:4`、`4:3`、`3:2`、`2:3`。对应默认尺寸是 1080×1920、1920×1080、1080×1080、1080×1350、1080×1440、1440×1080、1620×1080、1080×1620。
- 覆盖某一档尺寸时，宽和高都是正偶数，并且 `width * ratio_h == height * ratio_w`。不能增删比例档。
- `max_images` 的允许范围是 1 到 12。`max_file_bytes` 的允许范围是 1 到 20971520。等于默认值合法，大于默认值拒绝。
- 数据目录默认是进程当前目录下的 `.video-agent/`，环境变量 `VIDEO_AGENT_HOME` 可改。配置缺失时写入默认 `config.json`。配置读不出或违反规则时抛出错误，不改写原文件。
- 密钥只放在该目录的 `config.json`。仓库忽略 `.video-agent/`。
- 租约默认 60 秒，配置值不得低于 15 秒。自动退避只追加 2 次，常量名 `MAX_AUTO_RETRIES`。
- 配音语速只允许 0.5 到 2.0 倍。仍放不进已确认区间时，错误码是 `VOICEOVER_DOES_NOT_FIT`。
- 幂等记录只在状态写入成功的同一事务里插入。校验失败和状态冲突不写幂等记录。重放检查在状态判断之前。重放返回当时存下的 `TaskView`，不新建规划或视频执行。
- 帧数分摊并列时，改镜头列表里下标最小的镜头。
- 错误码只用规格里的表：`VALIDATION_ERROR`、`NOT_FOUND`、`STATE_CONFLICT`、`PLAN_VERSION_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`FIRST_FRAME_NOT_IMPLEMENTED`、`VOICEOVER_DOES_NOT_FIT`、`SHOT_DURATION_SHORT`、`ASSET_MISSING`、`PROVIDER_TEMPORARY`、`PROVIDER_REJECTED`、`PROVIDER_SUBMISSION_UNCERTAIN`、`MEDIA_EXPORT_FAILED`。不新增错误码。
- 不引入 Agent 编排框架，不实现 HTTP，不实现首帧生图，不写真实供应商报文。

## 标识和错误

`task_id`、`generation_id`、`artifact_id`、`asset_id`、`work_id` 都是 `uuid.uuid4()` 的字符串。

幂等键是方法参数，不进业务模型。合法形式是 1 到 128 个可见 ASCII：`^[\x21-\x7e]{1,128}$`。

```python
class VideoAgentError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        retryable: bool = False,
        task_id: str | None = None,
        generation_id: str | None = None,
        plan_version: int | None = None,
        fields: list[str] | None = None,
        shot_ids: list[str] | None = None,
        details: dict | None = None,
    ) -> None: ...
```

属性名与参数名相同。非法时长的 `details["duration_tiers"]` 是当前档位。非法比例的 `details["aspect_ratios"]` 是预设列表。`PLAN_VERSION_CONFLICT` 的 `details["head_plan_version"]` 是当前 head。再次确认已确认版本时，`generation_id` 是已有执行。

查询不到对象时 `code="NOT_FOUND"`。

## 配置

`video_agent.config.AppConfig` 是 Pydantic 模型。`load_config(home: Path) -> AppConfig`。`save_settings(home: Path, patch: SettingsPatch) -> AppConfig`。

默认 JSON 对象，键顺序固定为：

```json
{
  "mode": "fake",
  "duration_tiers": [5, 10, 15, 30],
  "default_duration": 10,
  "max_images": 12,
  "max_file_bytes": 20971520,
  "video_bitrate": 8000000,
  "audio_bitrate": 192000,
  "lease_seconds": 60,
  "resolutions": {
    "9:16": [1080, 1920],
    "16:9": [1920, 1080],
    "1:1": [1080, 1080],
    "4:5": [1080, 1350],
    "3:4": [1080, 1440],
    "4:3": [1440, 1080],
    "3:2": [1620, 1080],
    "2:3": [1080, 1620]
  },
  "adapters": {
    "planner": {"adapter": "fake"},
    "video": {"adapter": "fake"},
    "voice": {"adapter": "fake"},
    "music": {"adapter": "fake"},
    "visual": {"adapter": "none"}
  }
}
```

`SettingsPatch` 只含 `duration_tiers`、`default_duration`、`mode`、`planner`、`video`、`voice`、`music`、`visual`。后五项的类型是 `AdapterSetting`，字段为 `adapter: str` 和 `fields: dict[str, str]`。保存时合并适配器字段。键名包含 `key`、`token` 或 `secret` 的字段，传入空字符串表示保留原值。保存结果不合法时不写文件。

`mode="real"` 时，规划和视频的 `adapter` 必须存在于 `REAL_ADAPTERS`，不能是 `fake` 或 `none`，并且该适配器声明的必填项在合并后非空。配音、音乐、视觉可以是 `fake`、`none` 或已注册的真实适配器。本版 `REAL_ADAPTERS` 为空，因此切到 `real` 失败，消息说明没有已注册的真实规划或视频适配器。`fake` 模式不因真实字段为空而失败。

复制进工作和执行的配置形状：

```json
{"mode": "fake", "adapters": {"planner": {}, "video": {}, "voice": {}, "music": {}, "visual": {}}}
```

值是保存时的适配器对象，外加当时的 `mode`。

## 领域模型

模块 `video_agent.models`。

```python
class ProductInfo(BaseModel):
    name: str | None = None
    category: str | None = None
    selling_points: list[str] = []
    specs: str | None = None
    extra: dict[str, str] = {}

class VideoOptions(BaseModel):
    duration_seconds: int | None = None
    aspect_ratio: str = "9:16"
    subtitles: bool = True
    voiceover: bool = True
    music: bool = True

class AudioOptions(BaseModel):
    language: str | None = None
    voice_preference: str = ""
    music_direction: str = ""
    music_asset_id: str | None = None

class CreateTaskRequest(BaseModel):
    product_info: ProductInfo = ProductInfo()
    images: list[str] = []
    first_frame_asset_id: str | None = None
    user_prompt: str = ""
    video_options: VideoOptions = VideoOptions()
    audio_options: AudioOptions = AudioOptions()
    external_user_id: str | None = None
    external_request_id: str | None = None

class InputPatch(BaseModel):
    product_info: ProductInfo | None = None
    user_prompt: str | None = None
    images: list[str] | None = None
    first_frame_asset_id: str | None = None
    clear_first_frame: bool = False
    video_options: VideoOptions | None = None
    audio_options: AudioOptions | None = None

class SharedVisual(BaseModel):
    product_reference: str
    scene: str
    lighting: str
    palette: str
    style: str
    character: str | None = None
    scene_change: str | None = None

class ShotPlan(BaseModel):
    shot_id: str
    content: str
    action: str
    shot_size: str
    camera: str
    scene_id: str
    start_frame: int
    end_frame: int
    subtitle_text: str = ""
    voiceover_text: str = ""

class PlanDocument(BaseModel):
    creative: str
    audience: str
    selling_points: list[str]
    shared_visual: SharedVisual
    shots: list[ShotPlan]
    music_direction: str = ""
    music_asset_id: str | None = None
    duration_seconds: int
    aspect_ratio: str
    fit: Literal["native", "pad", "crop"]
    subtitles: bool
    voiceover: bool
    music: bool
    first_frame_source: Literal["selected", "generate"]
    first_frame_asset_id: str | None = None
    model_capabilities: str
    language: str | None = None
    voice_preference: str = ""

class ShotTextPatch(BaseModel):
    shot_id: str
    content: str | None = None
    action: str | None = None
    shot_size: str | None = None
    camera: str | None = None
    scene_id: str | None = None
    subtitle_text: str | None = None
    voiceover_text: str | None = None

class PlanPatch(BaseModel):
    duration_seconds: int | None = None
    aspect_ratio: str | None = None
    fit: Literal["native", "pad", "crop"] | None = None
    subtitles: bool | None = None
    voiceover: bool | None = None
    music: bool | None = None
    language: str | None = None
    voice_preference: str | None = None
    music_direction: str | None = None
    music_asset_id: str | None = None
    clear_music_asset: bool = False
    first_frame_asset_id: str | None = None
    clear_first_frame: bool = False
    shared_visual: SharedVisual | None = None
    shots: list[ShotPlan] | None = None
    shot_texts: list[ShotTextPatch] | None = None

class PlanChange(BaseModel):
    instruction: str | None = None
    patch: PlanPatch | None = None

class RegenerateRequest(BaseModel):
    kind: Literal["full", "shots"]
    artifact_id: str | None = None
    shot_ids: list[str] = []

class FailureView(BaseModel):
    code: str
    message: str
    stage: str | None = None
    retryable: bool

class TaskView(BaseModel):
    task_id: str
    status: str
    stage: str | None = None
    stage_detail: str | None = None
    failure_scope: str | None = None
    failure: FailureView | None = None
    questions: list[str] = []
    external_user_id: str | None = None
    external_request_id: str | None = None
    head_plan_version: int | None = None
    confirmed_plan_version: int | None = None
    generation_id: str | None = None
    completed_shots: int | None = None
    total_shots: int | None = None
    product_name: str | None = None
    product_info: ProductInfo = ProductInfo()
    user_prompt: str = ""
    video_options: VideoOptions = VideoOptions()
    audio_options: AudioOptions = AudioOptions()
    image_asset_ids: list[str] = []
    first_frame_asset_id: str | None = None
    updated_at: str

class TaskSummary(BaseModel):
    task_id: str
    status: str
    product_name: str | None
    updated_at: str

class PlanView(BaseModel):
    task_id: str
    plan_version: int
    is_head: bool
    confirmed: bool
    abandoned: bool
    document: PlanDocument

class GenerationView(BaseModel):
    generation_id: str
    task_id: str
    plan_version: int
    kind: Literal["full", "shots"]
    shot_ids: list[str]
    base_artifact_id: str | None
    status: str
    stage: str
    stage_detail: str | None = None
    completed_shots: int | None = None
    total_shots: int | None = None
    failure: FailureView | None = None

class ArtifactView(BaseModel):
    artifact_id: str
    task_id: str
    generation_id: str
    plan_version: int
    version: int
    duration_seconds: int
    aspect_ratio: str
    width: int
    height: int
    quality_status: Literal["passed", "flagged", "not_assessed"]
    quality: dict
    created_at: str

class TickResult(BaseModel):
    did_work: bool
    task_id: str | None = None
    step: str | None = None
```

`PlanChange` 必须恰好提供 `instruction` 或 `patch` 其中一个。两者都有或都没有，是 `VALIDATION_ERROR`。

`InputPatch.images is None` 表示不改图片。空列表表示清空。`clear_first_frame=True` 把首帧来源改为 `generate`。

`shots` 和 `shot_texts` 不能同时出现。`shots` 表示整表替换，必须自带合法帧区间。只改文案用 `shot_texts`，不改帧区间。

只改时长、镜头集合不变、又没有自带全套帧区间时，调用 `video_agent.frames.redistribute(lengths: list[int], new_total: int) -> list[int]`。`lengths` 是旧的各镜帧数。返回新的各镜帧数。镜头数大于 `new_total`，或无法在每镜至少 1 帧的前提下扣平，抛 `VideoAgentError("VALIDATION_ERROR")`。算法：按比例四舍五入后每镜至少 1 帧；总和多了，从当前最长且仍大于 1 的镜头扣，并列取最小下标；总和少了，加到当前最长镜头，并列取最小下标。

帧区间是半开区间，按镜头顺序首尾相接，并集为 `[0, duration_seconds * 30)`。

把字幕或配音从关改开时，补丁或上一版里必须已有对应非空文案。把音乐从关改开时，必须已有非空 `music_direction` 或未清除的 `music_asset_id`。关闭开关不删除文案。

结构化补丁改比例时，读取当前视频适配器的 `native_aspects`。补丁写了 `fit` 就用补丁。没写时，原生比例用 `native`，否则用 `pad`。结构化路径不判断裁切能不能保住主体。

## 服务签名

模块 `video_agent.service.VideoAgent`。

```python
def __init__(
    self,
    home: Path | None = None,
    *,
    clock: Clock | None = None,
    planner: Planner | None = None,
    video: VideoProvider | None = None,
    voice: VoiceProvider | None = None,
    music: MusicProvider | None = None,
    visual: VisualChecker | None = None,
) -> None: ...

def list_tasks(self) -> list[TaskSummary]: ...
def register_asset(self, *, filename: str, kind: Literal["image", "audio"], path: Path | None = None, data: bytes | None = None) -> str: ...
def create_task(self, request: CreateTaskRequest, *, idempotency_key: str | None = None) -> TaskView: ...
def get_task(self, task_id: str) -> TaskView: ...
def submit_inputs(self, task_id: str, patch: InputPatch, *, idempotency_key: str | None = None) -> TaskView: ...
def retry_planning(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView: ...
def get_plan(self, task_id: str, plan_version: int) -> PlanView: ...
def revise_plan(self, task_id: str, change: PlanChange, *, idempotency_key: str | None = None) -> TaskView: ...
def discard_revision(self, task_id: str, *, idempotency_key: str | None = None) -> TaskView: ...
def confirm(self, task_id: str, plan_version: int, *, idempotency_key: str | None = None) -> TaskView: ...
def regenerate(self, task_id: str, request: RegenerateRequest, *, idempotency_key: str | None = None) -> TaskView: ...
def get_generation(self, generation_id: str) -> GenerationView: ...
def retry_generation(self, generation_id: str, *, idempotency_key: str | None = None) -> TaskView: ...
def list_artifacts(self, task_id: str) -> list[ArtifactView]: ...
def artifact_path(self, artifact_id: str) -> Path: ...
def tick(self) -> TickResult: ...
def recover(self) -> int: ...
```

`home is None` 时使用 `VIDEO_AGENT_HOME`，再没有则使用 `Path.cwd() / ".video-agent"`。构造时创建目录、加载配置。配置非法时抛 `VideoAgentError`，不启动工人。

注入的适配器优先于配置。都没注入时，由 `video_agent.factory.build_adapters(config)` 按模式构造。`mode="fake"` 固定使用四个替身和 `visual=none`，不读适配器名字。

`video_agent.__init__` 导出 `VideoAgent` 和 `VideoAgentError`。

## 时钟、端口、工厂

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class SystemClock:
    def now(self) -> datetime: ...  # timezone-aware UTC

class FrozenClock:
    def __init__(self, start: datetime) -> None: ...
    def now(self) -> datetime: ...
    def advance(self, seconds: float) -> None: ...
```

时间序列化用 `datetime.isoformat()`。

```python
class VideoCapabilities(BaseModel):
    native_aspects: list[str]
    max_segment_seconds: int
    supports_first_frame: bool
    supports_reference_images: bool
    supports_tail_frame: bool

class PlanRequest(BaseModel):
    product_info: ProductInfo
    user_prompt: str
    images: list[str]
    duration_seconds: int
    duration_tiers: list[int]
    aspect_ratio: str
    subtitles: bool
    voiceover: bool
    music: bool
    audio: AudioOptions
    first_frame_asset_id: str | None
    video_capabilities: VideoCapabilities

class PlanResult(BaseModel):
    document: PlanDocument | None = None
    questions: list[str] | None = None

class ReviseResult(BaseModel):
    patch: PlanPatch | None = None
    questions: list[str] | None = None

class Planner(Protocol):
    def capabilities_note(self) -> str: ...
    def plan(self, request: PlanRequest) -> PlanResult: ...
    def revise(self, current: PlanDocument, instruction: str, request: PlanRequest) -> ReviseResult: ...

class ShotRequest(BaseModel):
    generation_id: str
    shot_id: str
    prompt: str
    shared_visual: SharedVisual
    first_frame_path: Path | None
    reference_paths: list[Path]
    tail_frame_path: Path | None
    frame_count: int
    aspect_ratio: str
    fit: Literal["native", "pad", "crop"]
    width: int
    height: int
    provider_idempotency_key: str

class SubmitResult(BaseModel):
    external_task_id: str | None = None
    uncertain: bool = False

class QueryResult(BaseModel):
    state: Literal["pending", "succeeded", "failed", "absent"]
    error_code: str | None = None
    message: str = ""

class VideoProvider(Protocol):
    def capabilities(self) -> VideoCapabilities: ...
    def submit_shot(self, request: ShotRequest) -> SubmitResult: ...
    def query(self, *, external_task_id: str | None, provider_idempotency_key: str) -> QueryResult: ...
    def download(self, external_task_id: str) -> bytes: ...

class VoiceCue(BaseModel):
    text: str
    start_ms: int
    end_ms: int

class VoiceResult(BaseModel):
    audio: bytes
    cues: list[VoiceCue]
    duration_ms: int

class VoiceProvider(Protocol):
    def synthesize(self, *, text: str, language: str | None, preference: str) -> VoiceResult: ...

class MusicProvider(Protocol):
    def generate(self, *, direction: str, duration_seconds: float) -> bytes: ...

class CheckFrame(BaseModel):
    shot_id: str
    position: Literal["start", "middle", "end", "boundary"]
    path: Path

class VisualResult(BaseModel):
    status: Literal["passed", "flagged", "not_assessed"]
    findings: list[dict] = []
    reason: str = ""

class VisualChecker(Protocol):
    def check(self, frames: list[CheckFrame], appearance: str) -> VisualResult: ...
```

`build_adapters(config: AppConfig) -> tuple[Planner, VideoProvider, VoiceProvider | None, MusicProvider | None, VisualChecker | None]`。

替身视频能力：`native_aspects=["9:16"]`，`max_segment_seconds=30`，三种参考都为真。替身默认产出足帧。短片段由测试注入的视频双倍制造，不改替身默认行为。

替身规划器把任何已登记且能用 Pillow 解码的图片当成可用产品参考。它不判断「看不清外观」。看不清、语言冲突之外的阻塞场景，由测试注入的规划器返回 `questions`。替身自己要实现的阻塞只有一种：配音或字幕开启，且产品资料与 `user_prompt` 的主语言冲突。主语言按汉字数和拉丁字母数比较，多的一方获胜；没有文字的一侧不参与。冲突时 `questions` 说明两侧语言，不发布方案。

替身规划不发明输入里没有的价格、功效、规格、认证和绝对化承诺。卖点逐字放进 `selling_points`。非 9:16 写 `fit="pad"`。时长小于等于 30 秒时一个镜头，标识 `s1`。自由描述里出现不在档位中的秒数时，不改 `duration_seconds`，把当前档位写进 `model_capabilities`。

`NoneVisual` 的 `check` 返回 `status="not_assessed"`、`reason="visual adapter is none"`。

## 文件库

`video_agent.files.LocalFileStore`。

```text
assets/{asset_id}/{filename}
tasks/{task_id}/generations/{generation_id}/first_frame{ext}
tasks/{task_id}/generations/{generation_id}/shots/{shot_id}.mp4
tasks/{task_id}/generations/{generation_id}/voice.m4a
tasks/{task_id}/generations/{generation_id}/music.m4a
tasks/{task_id}/generations/{generation_id}/cues.json
tasks/{task_id}/generations/{generation_id}/composed.mp4
tasks/{task_id}/generations/{generation_id}/frames/{shot_id}-{position}.png
tasks/{task_id}/generations/{generation_id}/final.mp4
```

`register_asset` 在字节写入文件库之后才返回 `asset_id`。`path` 和 `data` 恰好提供一个。图片魔数只接受 JPEG、PNG、WebP，并用 Pillow 解码。音频只接受 MP3、WAV、M4A。类型与 `kind` 不符、超过大小、解码失败，都是 `VALIDATION_ERROR`，不留文件。

## SQLite

库文件是 `{home}/agent.sqlite`。同一进程里的界面线程和工人线程用一把 `threading.Lock` 包住写事务。连接使用 `check_same_thread=False`。事务期间不调用 FFmpeg、Pillow 解码大图或网络。

```sql
CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  stage TEXT,
  stage_detail TEXT,
  failure_scope TEXT,
  failure_code TEXT,
  failure_message TEXT,
  failure_stage TEXT,
  failure_retryable INTEGER,
  failure_details_json TEXT,
  questions_json TEXT NOT NULL DEFAULT '[]',
  external_user_id TEXT,
  external_request_id TEXT,
  head_plan_version INTEGER,
  confirmed_plan_version INTEGER,
  generation_id TEXT,
  product_info_json TEXT NOT NULL,
  user_prompt TEXT NOT NULL DEFAULT '',
  video_options_json TEXT NOT NULL,
  audio_options_json TEXT NOT NULL,
  image_asset_ids_json TEXT NOT NULL,
  first_frame_asset_id TEXT,
  pending_instruction TEXT,
  planning_basis_json TEXT NOT NULL,
  completed_shots INTEGER,
  total_shots INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE plan_versions (
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  document_json TEXT NOT NULL,
  abandoned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, plan_version)
);

CREATE TABLE generations (
  generation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  kind TEXT NOT NULL,
  shot_ids_json TEXT NOT NULL,
  base_artifact_id TEXT,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  stage_detail TEXT,
  completed_shots INTEGER,
  total_shots INTEGER,
  snapshot_json TEXT NOT NULL,
  copied_config_json TEXT NOT NULL,
  failure_code TEXT,
  failure_message TEXT,
  failure_stage TEXT,
  failure_retryable INTEGER,
  failure_details_json TEXT,
  auto_attempts_used INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE generation_shots (
  generation_id TEXT NOT NULL,
  shot_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  start_frame INTEGER NOT NULL,
  end_frame INTEGER NOT NULL,
  status TEXT NOT NULL,
  provider_idempotency_key TEXT,
  external_task_id TEXT,
  submission_state TEXT,
  clip_path TEXT,
  PRIMARY KEY (generation_id, shot_id)
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  version INTEGER NOT NULL,
  duration_seconds INTEGER NOT NULL,
  aspect_ratio TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  relative_path TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  quality_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (task_id, version)
);

CREATE TABLE work_items (
  work_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  generation_id TEXT,
  ready_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  copied_config_json TEXT NOT NULL,
  step TEXT NOT NULL,
  next_attempt_at TEXT,
  done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE idempotency (
  method TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  task_id TEXT,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (method, idempotency_key)
);
```

`generation_shots.status` 取 `pending`、`submitted`、`imported`、`reused`、`failed`。`submission_state` 取 `intended`、`accepted`、`uncertain`、`absent`。

规划工作的 `step` 取 `start`、`finish`。生成工作的 `step` 取 `first_frame`、`submit_shot`、`import_shot`、`audio`、`compose`、`visual`、`export`。

`idempotency.method` 取 `create_task`、`submit_inputs`、`revise_plan`、`discard_revision`、`confirm`、`regenerate`、`retry_planning`、`retry_generation`。`request_hash` 是业务参数规范化 JSON 的 SHA-256。规范化使用 `json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`。幂等键本身不参与哈希。

## 状态写入

任务状态：`pending`、`planning`、`awaiting_input`、`awaiting_confirmation`、`generating`、`succeeded`、`failed`。

执行状态：`queued`、`running`、`succeeded`、`failed`。

`pending`、`planning`、`generating` 期间，补充、修改、确认、放弃、重生成和另一条重试都抛 `STATE_CONFLICT`。查询始终允许。

| 状态 | 允许的写入 |
| --- | --- |
| `awaiting_input` | `submit_inputs`；已有 `confirmed_plan_version` 时还允许 `discard_revision` |
| `awaiting_confirmation` | `revise_plan`、`confirm`；已有已确认版本时还允许 `regenerate` 和 `discard_revision` |
| `succeeded` | `revise_plan`、`regenerate` |
| `failed` 且 `failure_scope=planning` | `retry_planning`；从未发布方案时还允许 `submit_inputs`；已有 head 时还允许 `revise_plan`；已有已确认版本时还允许 `discard_revision` |
| `failed` 且 `failure_scope=generation` | `retry_generation`、`regenerate`、`revise_plan`；head 未确认时还允许 `confirm` 和 `discard_revision` |

没有 `confirmed_plan_version` 时，`regenerate` 和 `discard_revision` 抛 `STATE_CONFLICT`。用错恢复入口同样抛 `STATE_CONFLICT`。

`create_task`：图片为空则 `awaiting_input`，`questions=["缺少产品图片"]`，不插入工作。有图片则 `pending`，插入 `kind=planning`、`step=start` 的工作，并把当时模式和五项适配器抄进 `copied_config_json`。`first_frame_asset_id` 必须是本次图片中的一张；空值表示来源 `generate`。

`submit_inputs`：按补丁覆盖。处理后仍无图片则留在 `awaiting_input`，清空失败信息，`questions=["缺少产品图片"]`。至少一张合格图片则进入 `pending`，清空 `questions`，插入规划工作。

规划成功发布未确认 head，状态 `awaiting_confirmation`，`stage` 清空。阻塞问题不发布版本，状态 `awaiting_input`。结果不合法是 `PROVIDER_REJECTED`，`failure_scope=planning`，不发布版本。临时故障是 `PROVIDER_TEMPORARY`。自动退避期间保持 `planning`，不先写 `failed`。

结构化 `revise_plan` 当场克隆 head，套用补丁，发布新版本，状态 `awaiting_confirmation`。自然语言 `revise_plan` 把任务写成 `pending`，head 保持旧版，`pending_instruction` 记下这句话，并插入规划工作。

`discard_revision` 不创建执行。head 未确认时，把该版本标成放弃，head 回到 `confirmed_plan_version`。还没有新版本时，只取消这次等待或规划失败。状态改回该已确认版本最新一条视频执行的结果。

`confirm` 的版本必须是当前未确认 head。同一事务写入 `confirmed_plan_version` 和一条 `kind=full` 的执行，任务进入 `generating`，执行和任务阶段都是 `queued`，工作 `step=first_frame`。快照是当时的 `PlanDocument`。该版本已确认且本次不是原幂等键时，抛 `STATE_CONFLICT` 和已有 `generation_id`。旧版本、已放弃版本、已被取代的未确认版本抛 `PLAN_VERSION_CONFLICT`。

`regenerate` 的 `kind="full"` 绑定最新 `confirmed_plan_version`，不读草稿，不要求镜头标识，新建 `kind=full` 的执行。`kind="shots"` 要求 `artifact_id` 和至少一个 `shot_id`。镜头必须属于该成品的方案。即使包含全部镜头，记录仍然是 `shots`。基础成品不存在、镜头不属于该成品，或请求同时改时长、布局、场景、文案，都是 `VALIDATION_ERROR`，不创建执行。

执行成功且 head 仍是刚执行的已确认版本时，任务为 `succeeded`。执行成功但 head 已是更新的未确认草稿时，任务回到 `awaiting_confirmation`，成品照常入库。执行失败时任务为 `failed`，`failure_scope=generation`；未确认草稿还在时不改成待确认。

`retry_planning` 不读取调用里的新文案。没有方案时重放 `planning_basis_json`。失败来自自然语言修改时，重放 `pending_instruction` 和当时的 head。它把任务写回 `pending` 并新建规划工作。已有外部任务号的执行重试不在这里。

`retry_generation` 只恢复 `failure_retryable=1` 的那条执行，不新建执行。`retryable=false` 时抛 `STATE_CONFLICT`。`FIRST_FRAME_NOT_IMPLEMENTED` 和 `VOICEOVER_DOES_NOT_FIT` 的 `retryable` 为 false。

当前 `generation_id`：有未结束执行时指向它；否则指向最新一条；还没有执行时为空。`failed` 保留失败当时的 `stage`。离开 `failed` 时清空 `failure_scope` 和 `failure`。`completed_shots` 与 `total_shots` 只在 `generating` 期间有值。`stage` 只在 `planning` 和 `generating` 期间有值；`failed` 保留失败阶段。

成品版本从 1 按任务递增。`list_artifacts` 按 `version` 从新到旧。`list_tasks` 按 `updated_at` 从新到旧。

## 工人

`VideoAgent` 持有一个进程内 `worker_owner` UUID。

`claim_next` 在一个事务里领取 `done=0`、到期、且租约为空或已过期的最早 `ready_at`。写入 `lease_owner` 和 `lease_until=now+lease_seconds`。

`recover` 只清理 `lease_until < now` 的租约，返回清理条数。不重置 `step`、外部任务号和 `auto_attempts_used`。租约未到期的工作不领取。

```python
def hold_lease(db, work_id: str, owner: str, clock: Clock, lease_seconds: int):
    """上下文管理器。进入时启动守护线程，每 lease_seconds/2 秒把 lease_until 续到 now+lease_seconds。退出时停止线程。"""
```

阻塞的规划、下载和配音放在 `hold_lease` 内。`tick` 每次只推进一步，然后返回。外部任务未完成时，把 `next_attempt_at` 设好，续租，本步返回，不在 `tick` 里睡眠。没有工作时 `TickResult(did_work=False)`。

调用视频模型之前，先把 `provider_idempotency_key` 和 `submission_state="intended"` 写入 `generation_shots`。提交成功后写 `external_task_id` 和 `accepted`。无法确认是否创建时写 `uncertain`，错误码 `PROVIDER_SUBMISSION_UNCERTAIN`。之后的自动重试和执行重试只查询。查询结果是 `absent` 之后才允许再次提交。

`SHOT_DURATION_SHORT` 可自动退避，次数存在 `auto_attempts_used`，重启不重置。用尽后才把执行写成 `failed`。`ASSET_MISSING`、`PROVIDER_REJECTED`、`MEDIA_EXPORT_FAILED` 不自动退避，`retryable=true`。素材文件在执行时不存在，不重新提交视频模型。

## 媒体函数

模块 `video_agent.media`。计划 2 实现，计划 4 复用。

```python
def ffmpeg_available() -> bool: ...
def still_clip(image: Path, frames: int, width: int, height: int, dest: Path, *, video_bitrate: int) -> None: ...
def fit_image(image: Path, fit: Literal["native", "pad", "crop"], width: int, height: int, dest: Path) -> None: ...
def concat_clips(clips: list[Path], dest: Path) -> None: ...
def trim_or_loop_audio(src: Path, duration_seconds: float, dest: Path, *, audio_bitrate: int) -> None: ...
def burn_subtitles(video: Path, cues: list[VoiceCue], width: int, height: int, dest: Path, *, video_bitrate: int) -> None: ...
def mix_audio(video: Path, voice: Path | None, music: Path | None, cues: list[VoiceCue], dest: Path, *, video_bitrate: int, audio_bitrate: int) -> None: ...
def extract_png(video: Path, frame_index: int, dest: Path) -> None: ...
def probe(path: Path) -> ProbeInfo: ...
```

`ProbeInfo` 含 `frame_count`、`width`、`height`、`has_audio`、`video_codec`、`audio_codec`。

`pad` 等比缩放后放入目标画布，空余填黑。`crop` 等比放大后居中裁切。禁止拉伸。

字幕水平居中。底边距是高度的 8%，字号是高度的 4%，白色填充，2 像素黑色描边。一行超过画面宽度的 84% 时换行。换行后仍放不进画面，或 Windows 字体列表 `Microsoft YaHei`、`SimHei`、`Segoe UI`、`Arial` 都缺字形时，抛 `MEDIA_EXPORT_FAILED`。字体文件按这些家族名在 `C:\Windows\Fonts` 查找。

配音和音乐都关闭时，成片不包含音轨，`audio` 步不调用外部服务。只有已登记音乐时不调用音乐接口。音乐开启但两者都没有，或配音开启但没有配音实现，在 `audio` 步以 `PROVIDER_REJECTED` 失败。音乐裁切或循环到已确认时长。两者同时开启时，有人声的区间把音乐降到原音量的 25%，前后各 200 毫秒淡变；淡变重叠时取更低音量。无人声区间保持原音量。

成片先写临时文件。`probe` 证明可解码、帧数等于已确认秒数乘 30、尺寸符合方案、音轨与开关一致之后，才登记成品。模型片段长于镜头区间时裁到该区间。短于该区间时是 `SHOT_DURATION_SHORT`。

视觉检查抽取每个镜头的首、中、尾帧，以及相邻镜头连接处。抽取发生在字幕烧录之前。`flagged` 仍登记成品。检查不可用时记 `not_assessed` 并继续导出。适配器为 `none` 时仍然进入 `visual` 步。

局部重生成复用基础代的 `voice.m4a`、`music.m4a`、`cues.json` 和未替换镜头的无字幕片段，只重新生成被选中的镜头，再走 `compose`、`visual`、`export`。被替换镜头按时间顺序生成。左侧邻居也在本次替换中时，用刚生成片段的最后一帧；右侧邻居不在本次替换中时，用基础成品对应镜头的起始帧。只有一个镜头时不使用相邻帧。

## 桌面

窗口标题 `视频生成测试台`。顶栏、左侧任务列表、中间需求/方案/执行、底部最近一次错误。底部只在任务状态为 `failed` 时显示 `code`、`message`、`stage`。`awaiting_input` 的 `questions` 显示在中间，不显示成底部失败。

按钮状态放在 `desktop_app.viewmodel.button_states(...)`，测试不打开窗口。一次点击生成一个新的幂等键，方法返回前按钮保持不可点。FFmpeg 不在 PATH 时可以查看和打开已有成片，不能创建、确认、重生成或重试执行。

「提交修改」在自然语言句子非空时只提交 `PlanChange(instruction=...)`。句子为空时提交结构化 `PlanPatch`。按镜头重生成的勾选列表来自基础成品那一版方案。

界面每秒调用 `get_task` 和 `list_tasks`，直到当前任务离开 `pending`、`planning`、`generating`。桌面启动时先 `recover()`，再在工人线程循环 `tick()`；无工作或仍在等待时休眠 1 秒。

## 计划 1 的任务标题

1. 工程骨架和错误
2. 时钟
3. 配置
4. 领域模型
5. 帧数分摊
6. 文件库和素材登记
7. SQLite
8. 幂等
9. 创建任务和补充信息
10. 方案发布、修改和放弃
11. 确认和重生成记录
12. 重试入口的状态守卫
13. 规划工人
14. 查询

## 计划 2 的任务标题

1. 适配器工厂
2. 替身规划器
3. 媒体原语和替身视频
4. 替身配音和替身音乐
5. 首帧步骤
6. 提交和导入镜头
7. 音频、拼接、视觉检查和导出
8. 无窗口的指定首帧成片

计划 2 同时实现并测试八种开关，以及至少一种非 9:16 的 `pad` 导出。默认替身路径不制造短片段。

## 计划 3 的任务标题

1. 视图模型和按钮状态
2. 主窗口四区
3. 需求、方案和执行
4. 设置对话框
5. 工人线程和手工验收清单

## 计划 4 的任务标题

1. 整条重生成
2. 按镜头重生成
3. 失败阶段续跑和不可重试
4. 自动退避和租约
5. 外部提交不确定
6. 短片段、配音放不下和素材丢失

## 计划 3 已采纳的决议

桌面计划不新增 `video_agent` 的公开方法。

- 数据目录由 `desktop_app.viewmodel.resolve_home()` 解析：有 `VIDEO_AGENT_HOME` 用它，否则用 `Path.cwd() / ".video-agent"`。这个路径传给 `VideoAgent(home=...)`、`load_config` 和 `save_settings`。
- 设置对话框用内部函数从 `load_config` 读取适配器字段。键名包含 `key`、`token` 或 `secret` 的值在显示前清空。保存时空字符串仍交给 `save_settings`，由它保留原密钥。
- 选中任务时，需求区回填 `TaskView` 上的 `product_info`、`user_prompt`、`video_options`、`audio_options`、`image_asset_ids` 和 `first_frame_asset_id`。`product_name` 与 `product_info.name` 相同。镜头文案、比例适配和共享视觉设定来自 `get_plan` 的当前 head；没有 head 时，这些方案字段留空。没有重新选图时，后续 `InputPatch.images` 仍为 `None`，因为图片标识已经在任务视图里。

## 规格覆盖

计划作者在文末列一张表：规格验收条目 1 到 12，每条对应自己计划里的任务编号；不属于自己的条目标成「计划 N」。四份合起来必须覆盖 1 到 12。验收 12 只写成换适配器时的手工清单，不写报文。
