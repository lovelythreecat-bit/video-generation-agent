# 万相视频适配器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 真实出片可以在设置里改用阿里云百炼万相的首帧图生视频，火山方舟 Seedance 保持原样。

**Architecture:** 新增 `WanVideo`，注册为 `wan`。设置下拉选择供应商；单段上限和 `video_provider` 存在 `adapters.video` 的额外字段里。万相下载后按镜头宽高缩放，再交给现有裁切。

**Tech Stack:** Python 3.11+，Pydantic v2，Pillow，标准库，本机 ffmpeg / ffprobe。测试用 pytest 和假 HTTP。

## Global Constraints

- 规格：`docs/superpowers/specs/2026-10-11-wan-video-adapter-design.md`。
- 不改 Seedance 的请求、15 秒上限和 `provider-state/seedance-tasks.json`。
- 不接万相 2.7 的 `media` 接口。
- 设置里不加声音开关。有声无声只看 `ShotRequest.with_audio`。
- 请求分辨率固定 `720P`。不传比例、尾帧、参考图。
- `video_provider` 和 `max_segment_seconds` 只写在 `adapters.video`，都是字符串。
- 单段上限是 1 到 15 的整数。空值提示「视频生成还没填 单段最长秒数」。越界提示「视频生成的单段最长秒数要填 1 到 15 的整数」。
- 替身模式 `adapters.video.adapter` 仍是 `fake`，不调用视频接口。
- 不新增错误码。测试不访问外网。
- 不要 `git commit`。工作区里已有无关改动。

---

### Task 1: 万相适配器

**Files:**
- Create: `video_agent/wan_video.py`
- Create: `tests/video_agent/test_wan.py`
- Modify: `video_agent/factory.py`
- Modify: `video_agent/config.py`
- Modify: `tests/video_agent/test_factory.py`

**Interfaces:**
- Consumes: `request_json`、`request_bytes`、`ShotRequest`、`VideoCapabilities`、`media.probe`、`media.still_clip`
- Produces: `WanVideo(base_url, api_key, model, max_segment_seconds, state_path=None, transport=None, byte_transport=None)`，方法 `capabilities`、`submit_shot`、`query`、`download`。`config.wan_segment_error(raw: str) -> str | None`。`REAL_ADAPTERS["wan"]` 必填 `base_url`、`api_key`、`model`、`max_segment_seconds`。

- [ ] **Step 1: 写失败测试**

创建 `tests/video_agent/test_wan.py`，覆盖：`wan2.2-i2v-flash` 的 3 秒镜头提交 `duration` 5 且没有 `audio`；`wan2.6-i2v-flash` 的 6 秒镜头提交 `duration` 10 且 `audio` 跟随 `with_audio`；6 秒的 `wan2.2` 按镜头秒数 6 提交；同一幂等键不重复 POST；查询读 `output.task_status`，缺字段为 `pending`，`UNKNOWN` 和 HTTP 404 为 `absent`；下载缩放到 32×48 并保留音轨；缺首帧是 `PROVIDER_REJECTED`；缺任务号是 `PROVIDER_SUBMISSION_UNCERTAIN`。

在 `tests/video_agent/test_factory.py` 增加：`real` 模式、`adapter` 为 `wan`、带 `max_segment_seconds` 为 `"5"` 时，`build_adapters` 返回 `WanVideo`，`state_path` 以 `wan-tasks.json` 结尾，`capabilities().max_segment_seconds == 5`。

在 `tests/test_config.py` 增加：真实模式视频适配器为 `wan` 且 `max_segment_seconds` 为 `"0"` 或 `"1.5"` 时 `save_settings` 抛出 `VALIDATION_ERROR`，消息含「要填 1 到 15 的整数」，原文件不变。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/video_agent/test_wan.py tests/video_agent/test_factory.py::test_real_mode_builds_wan tests/test_config.py::test_wan_segment_rejects_out_of_range -v`

Expected: FAIL，`wan_video` 或 `test_real_mode_builds_wan` 尚不存在。

- [ ] **Step 3: 实现适配器、注册和校验**

`video_agent/config.py` 增加：

```python
def wan_segment_error(raw: str) -> str | None:
    text = raw.strip()
    if text == "":
        return "视频生成还没填 单段最长秒数"
    if not text.isdigit() or not 1 <= int(text) <= 15:
        return "视频生成的单段最长秒数要填 1 到 15 的整数"
    return None
```

`_FIELD_LABELS` 增加 `"max_segment_seconds": "单段最长秒数"`。`_assert_real_mode` 在汇总 `missing_fields` 之后、抛出之前：若视频适配器名是 `wan` 且 `max_segment_seconds` 去空白后非空，把 `wan_segment_error` 的非空结果放进 `missing_fields`。

`video_agent/factory.py` 增加 `_build_wan`，注册：

```python
"wan": AdapterSpec(
    ("base_url", "api_key", "model", "max_segment_seconds"),
    _build_wan,
),
```

`_build_wan` 把 `state_path` 设为 `home / "provider-state" / "wan-tasks.json"`（`home` 为 `None` 时不设）。

`video_agent/wan_video.py` 的行为：

- 原生比例：`9:16`、`16:9`、`1:1`、`4:5`、`3:4`、`4:3`、`3:2`、`2:3`。支持首帧。不支持参考图和尾帧。`max_segment_seconds` 用 `wan_segment_error` 校验后转成整数，不合法就抛 `VALIDATION_ERROR`。
- 提交地址 `{根}/services/aigc/video-generation/video-synthesis`。根已经以该后缀结尾则不追加。查询地址 `{根}/tasks/{task_id}`；根含提交后缀时先去掉再拼。
- POST 超时 60 秒，头含 `Authorization: Bearer`、`Content-Type: application/json`、`X-DashScope-Async: enable`，`uncertain=True`。
- 正文 `input.prompt` 原样，`input.img_url` 与 Seedance 相同的 JPEG data URL。`parameters` 固定 `resolution=720P`、`prompt_extend=False`、`watermark=False`。
- 镜头秒数 `max(1, round(frame_count / 30))`。模型名小写后：含 `wan2.2` 或 `wanx2.1` 时允许档只有 5，且不传 `audio`；否则含 `wan2.5` 或 `wan2.6` 时允许档是 5 和 10，并传 `audio=with_audio`；其他名字 `duration` 等于镜头秒数且不传 `audio`。有不短于镜头秒数的允许档时取最小的一档，否则 `duration` 等于镜头秒数。
- 没有首帧文件：`PROVIDER_REJECTED`，消息「视频生成需要首帧图片」，`retryable=True`。
- 任务号优先 `output.task_id`，其次顶层或 `output` 里的 `id`、`task_id`。没有任务号：`PROVIDER_SUBMISSION_UNCERTAIN`。
- 状态文件是 JSON 对象，键是幂等键，值是 `{"task_id","width","height"}`。同一幂等键已有任务号时直接返回，不再 POST。
- 查询只读 `output.task_status`。没有该字段是 `pending`。`PENDING`、`RUNNING` 是 `pending`。`SUCCEEDED` 是 `succeeded`。`FAILED`、`CANCELED`、`CANCELLED` 是 `failed` / `PROVIDER_REJECTED`。`UNKNOWN` 或 HTTP 404 是 `absent`。失败说明取 `message` 或 `code`，最长 300 字。
- 下载读 `output.video_url`。没有地址：`PROVIDER_TEMPORARY`。用记下的宽高做 ffmpeg 缩放：`scale=width:height`，视频 `libx264`、`yuv420p`、30 fps；原来有音轨就保留 AAC，没有就 `-an`。缩放结果作为 `download` 的返回字节。Seedance 代码不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/video_agent/test_wan.py tests/video_agent/test_factory.py tests/test_config.py -v`

Expected: PASS

- [ ] **Step 5: 不要提交**

工作区已有无关改动。不要 `git add`，不要 `git commit`。

### Task 2: 设置里选择供应商

**Files:**
- Modify: `desktop_app/settings_dialog.py`
- Modify: `tests/test_settings_form.py`

**Interfaces:**
- Consumes: `compose_settings_edit`、`missing_api_gaps`、`wan_segment_error`
- Produces: `compose_settings_edit(..., video_provider: str = "seedance", max_segment_seconds: str = "")`。真实且 `video_provider=="wan"` 时视频适配器名是 `wan`，字段含 `video_provider` 和 `max_segment_seconds`。真实且为 `seedance` 时适配器名仍是 `seedance`，字段含 `video_provider=seedance`，不要求单段上限。替身时适配器名是 `fake`，字段含 `video_provider`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_settings_form.py` 增加：

- 现有调用不传 `video_provider` 时，真实模式视频适配器仍是 `seedance`，并且 `fields["video_provider"]=="seedance"`。
- `video_provider="wan"`、`max_segment_seconds="5"`、真实模式时，适配器名是 `wan`，两个字段都写入。
- 真实万相且单段上限为 `""` 时，`missing_api_gaps` 含「还没填 单段最长秒数」。值为 `"16"` 时含「要填 1 到 15 的整数」。
- 替身模式适配器名是 `fake`，不要求接口字段，`fields["video_provider"]` 等于传入值。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_settings_form.py -v`

Expected: FAIL，`compose_settings_edit` 还不接受 `video_provider`，或字段断言失败。

- [ ] **Step 3: 改设置窗口**

`compose_settings_edit` 增加关键字参数 `video_provider: str = "seedance"` 和 `max_segment_seconds: str = ""`。真实模式的视频适配器名用 `video_provider`（只允许 `seedance` 或 `wan`，其他值按 `seedance`）。替身模式视频适配器名仍是 `fake`。视频字段在传入的 `video` 上写入 `video_provider`。`video_provider=="wan"` 时再写入 `max_segment_seconds`。

`missing_api_gaps` 在真实模式、视频字段的 `video_provider` 为 `wan` 时调用 `wan_segment_error`。空值和越界都放进返回列表。Seedance 不查这个字段。

设置窗口「视频生成」在地址栏之前加只读下拉「供应商」，选项「火山方舟 Seedance」「阿里云百炼万相」。选万相时显示「单段最长秒数」输入框，选 Seedance 时隐藏。Seedance 的说明是：地址填 `https://ark.cn-beijing.volces.com/api/v3`。API Key 和模型名到方舟控制台复制。万相的说明是：地址填到业务空间的 `/api/v1`，例如 `https://llm-xxx.cn-beijing.maas.aliyuncs.com/api/v1`。模型名自己填，例如 `wan2.2-i2v-flash`。单段最长秒数是这一模型一次调用的上限，填 1 到 15 的整数。更长的成片会拆成多段。打开窗口时：适配器名是 `seedance` 或 `wan` 就用它；否则用字段 `video_provider`，没有则是 `seedance`。保存时把下拉和秒数传给 `compose_settings_edit`。新建任务界面不改。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_settings_form.py tests/test_viewmodel.py tests/video_agent/test_factory.py tests/video_agent/test_wan.py tests/test_config.py -v`

Expected: PASS

- [ ] **Step 5: 不要提交**

不要 `git add`，不要 `git commit`。
