# 阿里云百炼万相视频适配器

日期：2026 年 10 月 11 日  
状态：待评审

真实出片可以继续用火山方舟 Seedance，也可以改用阿里云百炼万相的首帧图生视频。两家各有一个适配器。设置里选定当前用哪一家。

## 目标

桌面测试程序在「视频生成」里增加供应商选择。选万相时，再填写这一家一次调用最长能生成的秒数。新建任务的界面不出现这个秒数。成片总时长仍由任务的时长档决定。

万相适配器把已经确定的首帧和镜头提示词交给百炼，异步取回 MP4。替身模式仍不调用任何视频接口。

## 不做

- 不改 Seedance 的请求、15 秒单段上限和任务状态文件。
- 不接万相 2.7 的多模态 `media` 接口。模型名写成 `wan2.7` 时仍按 `img_url` 首帧接口提交，服务拒绝就记为供应商错误。模型名包含 `wan3.0` 时只提交一种 `media`：`first_frame`。不接尾帧、参考图、视频编辑和视频延长。
- 不在设置里增加声音开关。有声或无声只看任务。
- 不在新建任务、方案或成片界面展示供应商和单段上限。
- 不调用百炼的真实接口做测试。测试注入假 HTTP。

## 设置

「视频生成」增加下拉「供应商」，两项为「火山方舟 Seedance」和「阿里云百炼万相」。地址、API Key、模型名两家都填。选万相时显示「单段最长秒数」。选 Seedance 时不显示这一栏。

说明文字随供应商变化：

- Seedance：地址填 `https://ark.cn-beijing.volces.com/api/v3`。API Key 和模型名到方舟控制台复制。
- 万相：地址填到业务空间的 `/api/v1`，例如 `https://llm-xxx.cn-beijing.maas.aliyuncs.com/api/v1`。模型名自己填，例如 `wan2.2-i2v-flash`。万相 3.0 填 `wan3.0-video` 或 `wan3.0-video-prime`。单段最长秒数是这一模型一次调用的上限，填 1 到 15 的整数。更长的成片会拆成多段。

同一时间只保存当前选中的供应商。改下拉后，地址、密钥和模型名留在输入框里，保存前改成这一家的值。密钥留空再保存，沿用这一角色里已经保存的密钥。

`video_agent/config.py` 的 `config_to_dict` 不会保留配置根上的新键。`video_provider` 和 `max_segment_seconds` 都是 `adapters.video` 的额外字段，随 `base_url`、`api_key`、`model` 这些现有适配器字段一起保存，值都是字符串。

每次保存视频设置都写入 `adapters.video.video_provider`，值为当前下拉项：`seedance` 或 `wan`。出片方式为替身时，`adapters.video.adapter` 仍是 `fake`，不调用万相或 Seedance；选万相时同时写入 `adapters.video.max_segment_seconds`。打开设置时，下拉项从 `adapters.video.video_provider` 恢复；没有这个字段时视为 `seedance`。之后改成真实出片时，适配器名用这份 `video_provider`，字段不存在时同样视为 `seedance`。出片方式已经是真实时，以 `adapters.video.adapter` 为准，取值就是 `seedance` 或 `wan`。

出片方式为真实时：

| 供应商 | `adapters.video.adapter` | 必填 |
| --- | --- | --- |
| 火山方舟 Seedance | `seedance` | `base_url`、`api_key`、`model` |
| 阿里云百炼万相 | `wan` | `base_url`、`api_key`、`model`、`max_segment_seconds` |

`max_segment_seconds` 在配置里是字符串，内容必须是 1 到 15 的整数，不能有小数、空格或正负号。空值在真实出片下保存失败，提示还没填单段最长秒数。超出范围或不是整数时保存失败，提示要填 1 到 15 的整数。Seedance 不读取这个字段。替身出片不要求这个字段。

## 万相适配器

模块 `video_agent.wan_video`，类 `WanVideo`。在 `REAL_ADAPTERS` 注册为 `wan`。任务号记在数据目录的 `provider-state/wan-tasks.json`。同一个 `provider_idempotency_key` 已经有任务号时，再次提交直接返回该任务号，不再请求。提交成功时，把这次镜头的宽高和任务号写在同一条记录里。

能力：

| 字段 | 值 |
| --- | --- |
| `native_aspects` | `9:16`、`16:9`、`1:1`、`4:5`、`3:4`、`4:3`、`3:2`、`2:3` |
| `max_segment_seconds` | 设置里的单段最长秒数 |
| `supports_first_frame` | 真 |
| `supports_reference_images` | 假 |
| `supports_tail_frame` | 假 |

比例由已经适配好的首帧决定，请求里不传比例。请求参数仍然是 `720P`。不传尾帧，不传参考图。没有可读的首帧时拒绝提交，错误码 `PROVIDER_REJECTED`，说明视频生成需要首帧图片，可以重试。

接口根地址去掉末尾斜杠。提交地址是 `{根地址}/services/aigc/video-generation/video-synthesis`。根地址已经以该路径结尾时不再追加。查询和下载任务详情用 `{根地址}/tasks/{task_id}`；根地址含有提交路径时，先去掉这段路径再拼查询地址。

提交使用 `POST`，超时 60 秒。查询超时 30 秒，下载超时 120 秒。提交请求头为 `Authorization: Bearer {api_key}`、`Content-Type: application/json`、`X-DashScope-Async: enable`。查询任务和下载前读取任务详情只带 `Authorization` 和 `Content-Type`，不带 `X-DashScope-Async`。这个头只属于创建任务。查询带上它时，百炼返回 HTTP 403，原文是 `current user api does not support asynchronous calls`，任务本身已经创建成功。模型名不含 `wan3.0` 时正文：

```json
{
  "model": "配置中的模型名",
  "input": {
    "prompt": "镜头提示词",
    "img_url": "data:image/jpeg;base64,..."
  },
  "parameters": {
    "resolution": "720P",
    "duration": 5,
    "prompt_extend": false,
    "watermark": false
  }
}
```

模型名包含 `wan3.0` 时（比较忽略大小写），`input` 只含提示词和 `media`，不再提交 `img_url`。`media` 只有一个元素，`type` 为 `first_frame`，`url` 仍是 JPEG data URL。`parameters` 仍是 `720P`、`prompt_extend` 为假、`watermark` 为假，不传 `ratio`。标准版 `wan3.0-video` 与高速版 `wan3.0-video-prime` 正文形状相同：

```json
{
  "model": "wan3.0-video",
  "input": {
    "prompt": "镜头提示词",
    "media": [
      {
        "type": "first_frame",
        "url": "data:image/jpeg;base64,..."
      }
    ]
  },
  "parameters": {
    "resolution": "720P",
    "duration": 5,
    "prompt_extend": false,
    "watermark": false,
    "audio": false
  }
}
```

首帧编码与 Seedance 相同：转成 RGB JPEG，最长边不超过 1280，质量 85，再做成 data URL。提示词原样提交，适配器不截断、不改写。`prompt_extend` 固定为假，`watermark` 固定为假。

镜头秒数与 Seedance 相同，为 `max(1, round(帧数 / 30))`。`duration` 按模型名决定，模型名比较时忽略大小写：

| 模型名包含 | 允许的秒数 | `duration` |
| --- | --- | --- |
| `wan2.2` 或 `wanx2.1` | 5 | 存在不短于镜头秒数的允许值时，取其中最小的一个 |
| `wan2.5` 或 `wan2.6` | 5、10 | 同上 |
| `wan3.0` | 2 到 30 | 镜头秒数小于 2 时提交 2；2 到 30 含边界时提交镜头秒数；大于 30 时仍提交镜头秒数，由服务拒绝 |
| 其他名字 | 无固定档 | 等于镜头秒数 |

没有不短于镜头秒数的允许值时，`duration` 等于镜头秒数，由服务拒绝。返回视频长于镜头时，沿用现有裁切，把多余帧去掉。短于镜头时，沿用现有帧数检查，错误码 `SHOT_DURATION_SHORT`，阶段是生成镜头。

`video_agent/pipeline.py` 的 `_assert_final` 要求成片宽高等于配置画布，默认 9:16 是 1080×1920。`_trim_clip` 和 `concat_clips` 都不缩放。万相下载得到的 MP4，在交给现有裁切之前，缩放到这次镜头的宽高，也就是提交时的 `ShotRequest.width` 和 `ShotRequest.height`。`download` 只接收任务号，所以宽高要在提交成功时和任务号一起记下，下载时再缩放。缩放保留原来的音轨。Seedance 的下载结果仍原样写入，不改流水线。

声音只看 `ShotRequest.with_audio`：

- 模型名包含 `wan3.0` 时，始终传 `parameters.audio`，值等于 `with_audio`。有声为真，无声为假。无声任务必须显式传假，不能靠服务默认值出声。
- 模型名包含 `wan2.5` 或 `wan2.6` 时，`parameters.audio` 等于 `with_audio`。有声为真，无声为假。
- 其他模型名不传 `audio`。有声而服务没有音轨时，沿用现有成片规则：片段没有音轨就补本地占位声音。

`wan2.2` 同时包含在第一张表里。名字同时像两类时，按 `wan2.2`、`wanx2.1` 处理，不传 `audio`，允许秒数只有 5。模型名包含 `wan3.0` 时不进入这两类。

提交成功时读取任务号，字段优先 `output.task_id`，也接受与 Seedance 相同的 `id`、`task_id` 位置。没有任务号时错误码为 `PROVIDER_SUBMISSION_UNCERTAIN`，可以重试。查询状态只读 `output.task_status`。没有这个字段时视为 `pending`。只有值为 `UNKNOWN` 或 HTTP 404 时才是 `absent`。

| 服务状态 | 适配器状态 |
| --- | --- |
| 没有 `output.task_status`，或 `PENDING`、`RUNNING` | `pending` |
| `SUCCEEDED` | `succeeded` |
| `FAILED`、`CANCELED`、`CANCELLED` | `failed`，错误码 `PROVIDER_REJECTED` |
| `UNKNOWN`，或 HTTP 404 | `absent` |

失败说明取服务返回的 `message` 或 `code`，最长 300 字。下载在成功结果的 `output.video_url` 取 MP4 字节，再按记下的宽高缩放，并保留原来的音轨。还没有地址时错误码为 `PROVIDER_TEMPORARY`，可以重试。

## 测试

- 提交正文包含模型名、首帧 data URL、720P、关闭改写、关闭水印，以及按模型名算出的 `duration` 和是否出现 `audio`。模型名包含 `wan3.0` 时正文是 `input.media` 里的一个 `first_frame`，没有 `img_url`，不传 `ratio`，并始终带 `audio`。约 1 秒提交 `duration` 2，6 秒提交 6。`wan2.2`、`wan2.5`、`wan2.6` 和其他名字仍是 `img_url`。
- 短于允许档的镜头向上取档。长于全部允许档的镜头按镜头秒数提交。
- 同一幂等键不重复提交。查询只读 `output.task_status`，覆盖排队、成功和失败。没有该字段视为排队。`UNKNOWN` 或 HTTP 404 视为不存在。成功后能从 `video_url` 下载，并缩放到提交时记下的宽高，音轨保留。
- 缺少首帧、缺少任务号，各自返回上面的错误码。
- 真实设置选万相时适配器名为 `wan`，单段上限为空或越界不能保存。选 Seedance 时适配器名仍为 `seedance`，且不要求单段上限。替身模式不要求接口字段，`adapter` 仍是 `fake`，并把 `video_provider` 写在 `adapters.video`。打开设置时从该字段恢复下拉项，字段不存在视为 `seedance`。
- 测试不访问外网。
