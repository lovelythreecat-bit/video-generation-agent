# Video Agent API 使用与部署

本服务复用 VideoAgent 业务状态机。正式任务和模型配置存入 MySQL 5.7+，媒体文件存入 `VIDEO_AGENT_HOME`；HTTP 与 Worker 是两个独立进程。HTTP 不启动工人，不在启动时导入旧 SQLite/JSON。默认监听本机 `127.0.0.1:8000`。

## 安装与初始化

要求 Python 3.11+、MySQL 5.7+（InnoDB、utf8mb4）以及能通过 PATH 运行的 FFmpeg 和 ffprobe。MySQL 5.7 使用 `FOR UPDATE` 领取任务，等待其他事务释放行锁后重新检查任务租约；MySQL 8.0.1+ 使用 `FOR UPDATE SKIP LOCKED` 跳过已锁定任务。MariaDB 不作为支持的后端。安装：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e '.[dev]'
```

由数据库管理员建立一个新的目标库与受限应用账号。先核实本机版本与同名库，避免复用已有业务库。示例 DDL 不包含账号密码：

```sql
SELECT VERSION();
CREATE DATABASE video_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
```

### Windows 一键启动

项目根目录的 `.env` 是本地启动配置，已被 Git 忽略。新环境先复制 [配置模板](../../.env.example) 为 `.env`，填写数据库连接、两个不同的非空 Token 和 Fernet 主密钥；本机已有配置时直接修改，保留原有 Token 和主密钥。

双击根目录的 [start-webapi.bat](../../start-webapi.bat) 即可启动 WebAPI，也可在终端运行：

```powershell
.\start-webapi.bat
```

启动后访问 http://127.0.0.1:8000/docs，按 Ctrl+C 停止。脚本使用项目的 `.venv/Scripts/python.exe`，请先完成上面的依赖安装。

常用配置如下：

| 配置项 | 用途 / 默认值 |
| --- | --- |
| `VIDEO_AGENT_API_HOST` | 监听地址，默认 `127.0.0.1` |
| `VIDEO_AGENT_API_PORT` | HTTP 端口，默认 `8000` |
| `VIDEO_AGENT_API_LOG_LEVEL` | 日志级别，默认 `info` |
| `VIDEO_AGENT_DB_HOST` / `VIDEO_AGENT_DB_PORT` | MySQL 地址 / 端口 |
| `VIDEO_AGENT_DB_NAME` | 数据库名，默认 `video_agent` |
| `VIDEO_AGENT_DB_USER` / `VIDEO_AGENT_DB_PASSWORD` | 数据库账号 / 密码 |
| `VIDEO_AGENT_HOME` | 媒体目录，默认项目下的 `.video-agent`；相对路径按项目根目录解析 |
| `VIDEO_AGENT_BUSINESS_TOKEN` / `VIDEO_AGENT_ADMIN_TOKEN` | 业务 / 管理 Token，必须非空且不同 |
| `VIDEO_AGENT_SECRET_KEY` | 持久保存的 Fernet 加密主密钥 |

配置格式为每行 `KEY=value`，支持整行 `#` 注释和成对单引号或双引号。值按原文读取，`!`、`$`、`#` 和 `=` 等字符不会被执行或展开；不支持行尾注释。文件中的值覆盖当前启动进程继承的同名环境变量，不修改系统环境变量。

Worker 和迁移可使用同一份 `.env`：

```powershell
# 首次建库后或版本升级时显式执行迁移；正常启动不自动修改 schema。
.\start-webapi.bat -Mode migrate
# 在另一个终端启动 Worker，使异步任务能够执行。
.\start-webapi.bat -Mode worker
# 使用指定配置文件。
.\start-webapi.bat -EnvFile 'D:\config\video-agent.env'
```

### 手动启动

不使用上述脚本时，按 [环境变量模板](../../.env.example) 在进程环境注入连接字段、两个不同的非空 Token、Fernet 主密钥和共同文件根目录。Python 入口自身仍只读取进程环境变量。`VIDEO_AGENT_HOME` 请设置成绝对目录；API、Worker 和桌面必须指向同一目录。数据库连接使用独立 host/port/name/user/password 字段，密码特殊字符无需 URL 转义。

可在安全的本机环境生成主密钥并直接写入当前进程变量；不要打印或提交它：

```powershell
$env:VIDEO_AGENT_SECRET_KEY = (& .venv/Scripts/python.exe -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
$env:VIDEO_AGENT_BUSINESS_TOKEN = (& .venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(32))')
$env:VIDEO_AGENT_ADMIN_TOKEN = (& .venv/Scripts/python.exe -c 'import secrets; print(secrets.token_urlsafe(32))')
```

把主密钥安全保存到独立的秘密存储，再让两个进程继承相同的值。数据库备份必须配合同一主密钥恢复；丢失或更换主密钥会使历史秘密与快照不可解密。不要在重启时重新生成主密钥。

数据库与环境准备好后，显式升级 schema：

```powershell
.venv/Scripts/python.exe -m alembic upgrade head
```

迁移只为全新设置初始化一次 fake 默认配置，后续升级不会重置当前设置。API/Worker 正常启动不建表。分别启动两个进程：

```powershell
.venv/Scripts/python.exe -m video_agent.worker_main
.venv/Scripts/python.exe -m uvicorn api_app.app:create_app --factory --host 127.0.0.1 --port 8000
```

两个命令需要分别运行在终端或进程管理器中，并继承相同环境。首版只运行一个 Worker；数据库单运行者租约为 30 秒，每 5 秒续期。正常停止 Worker 后排队任务保留，崩溃重启会安全恢复过期工作项。

## 在线文档与认证

- [Swagger UI](http://127.0.0.1:8000/docs)：浏览接口并 Try it out；Authorize 中仅填写 Token 值。
- [ReDoc](http://127.0.0.1:8000/redoc)：完整接口手册。
- [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)：可导入 Postman、Apifox 等工具。

三个入口公开可读。Swagger UI 和 ReDoc 的真实 JS/CSS 随 Python 包本地提供，页面不依赖外部 CDN、Google Fonts 或验证服务。Swagger 不预填、不持久保存 Token；请求仍执行服务认证。接口、模型、状态码和中文说明由实际代码生成。

业务及管理 Token 都可以调用业务 API，只有管理 Token 能调用 `/api/v1/admin/*`。缺失或错误 Token 返回 401，业务 Token 访问管理接口返回 403。调用方共享任务空间，`external_user_id` 和 `external_request_id` 是业务筛选标识，不构成权限隔离。

## 上传、规划、生成与下载

下面 PowerShell 示例只从环境变量读取 Token；`product.png` 为调用方图片，所有 URL 均指向本机服务。

```powershell
$base = 'http://127.0.0.1:8000'
$headers = @{ Authorization = "Bearer $env:VIDEO_AGENT_BUSINESS_TOKEN" }
$asset = Invoke-RestMethod "$base/api/v1/assets" -Method Post -Headers $headers -Form @{
    kind = 'image'
    file = Get-Item './product.png'
}
$body = @{
    images = @($asset.asset_id)
    first_frame_asset_id = $asset.asset_id
    product_info = @{ name = '示例保温杯'; selling_points = @('轻便', '便携') }
    user_prompt = '展示商品与使用场景'
    video_options = @{ duration_seconds = 5; aspect_ratio = '9:16'; subtitles = $false; voiceover = $false; music = $false }
} | ConvertTo-Json -Depth 10
$task = Invoke-RestMethod "$base/api/v1/tasks" -Method Post -Headers ($headers + @{ 'Idempotency-Key' = 'example-task-001' }) -ContentType 'application/json' -Body $body
$task = Invoke-RestMethod "$base/api/v1/tasks/$($task.task_id)" -Headers $headers
```

创建返回 201，不等待规划。轮询最后一个 GET：`awaiting_input` 时读取 `questions`，用 `PATCH /tasks/{task_id}/inputs` 补充资料；`awaiting_confirmation` 时 `head_plan_version` 可供读取。不要在首次 GET 尚未产生方案时立即确认：

```powershell
$plan = Invoke-RestMethod "$base/api/v1/tasks/$($task.task_id)/plans/$($task.head_plan_version)" -Headers $headers
$confirm = @{ plan_version = $task.head_plan_version } | ConvertTo-Json
$task = Invoke-RestMethod "$base/api/v1/tasks/$($task.task_id)/confirm" -Method Post -Headers ($headers + @{ 'Idempotency-Key' = 'example-confirm-001' }) -ContentType 'application/json' -Body $confirm
$generation = Invoke-RestMethod "$base/api/v1/generations/$($task.generation_id)" -Headers $headers
```

确认返回 202。轮询 generation 到 `succeeded`，然后读取和下载成片：

```powershell
$artifacts = Invoke-RestMethod "$base/api/v1/tasks/$($task.task_id)/artifacts" -Headers $headers
Invoke-WebRequest "$base/api/v1/artifacts/$($artifacts[0].artifact_id)/content" -Headers $headers -OutFile './output.mp4'
```

下载支持标准 `Range: bytes=0-1023`，返回 206 与 Content-Range，越界返回 416。资产和成片响应不返回服务端文件路径；请求只能通过 ID 下载。图片接受 JPEG/PNG/WebP，音频接受 MP3/WAV/M4A。上传忽略伪造 Content-Length，按实际文件字节读取至上限加一即拒绝（413）；实际限制可从 `/api/v1/capabilities` 查询。图片同时验证文件签名与实际解码。

替身视频目前需要首帧，因此示例把上传素材同时设为 `first_frame_asset_id`。fake 模式生成也使用真实 FFmpeg；不调用付费供应商。

## 修改与重试

`POST /tasks/{id}/plan-revisions` 接受 `{"patch":{"music":false}}` 或 `{"instruction":"改成简洁风格"}`。结构化修改返回 200，自然语言修改入队后返回 202。`POST /tasks/{id}/discard-revision` 放弃未确认修改并回到已确认基线；没有已确认版本时返回 409。

规划失败用 `POST /tasks/{id}/retry-planning`，生成失败用 `POST /generations/{id}/retry`。成功成片后可 `POST /tasks/{id}/regenerations`，body 为 `{"kind":"full"}` 或 `{"kind":"shots","artifact_id":"示例成片ID","shot_ids":["s1"]}`。这些异步操作返回 202，非法状态返回 409。不提供取消或删除任务接口。

所有任务状态变更以及创建模型配置均支持 `Idempotency-Key`，长度 1～128、仅可见 ASCII。相同方法与键、相同参数重放已有结果；不同参数返回 409。重试网络请求时保留原键，新的业务操作使用新键。

任务列表支持 `limit=20`、`offset=0`、`status`、`external_user_id`、`external_request_id`；筛选精确且区分大小写，limit 最大 100。返回 `{items,limit,offset,total}`，按 `updated_at DESC, task_id DESC` 在数据库分页。配置列表使用相同分页结构。

## 模型配置与公共设置

先用管理 Token 调用 `GET /api/v1/admin/adapter-types`，查询已注册 adapter、role、required_fields 与 secret_fields。然后创建模型配置；请求字段示例（秘密值通过调用方秘密存储提供）：

```json
{"name":"规划模型 A","role":"planner","adapter":"openai_chat","base_url":"https://example.invalid/v1","model":"example-model","parameters":{},"secrets":{},"enabled":true}
```

`secrets` 只写不读。响应只提供 `has_api_key`、`secret_presence` 标记，不返回明文、可恢复掩码或密文。参数内不能夹带秘密，base_url 不能包含凭据。名称可以相同，以 profile_id 区分配置。

读取 `GET /admin/adapter-bindings` 获取 `configuration_revision`，再 PATCH 同一资源：

```json
{"expected_version":1,"planner":{"profile_id":"示例规划配置ID"},"video":{"profile_id":"示例视频配置ID"}}
```

实际 expected_version 必须使用刚读取的修订。整组绑定一起校验；停用配置、角色不兼容或版本过期会整组失败。fake/none 绑定用 `{"adapter":"fake"}` 或 `{"adapter":"none"}`，具体角色支持由服务验证。绑定中的配置不能停用或删除，先切换绑定。

准备好 planner 和 video 的真实绑定与必要参数后，读取 `GET /admin/settings` 的新 configuration_revision，再 PATCH `{"expected_version":2,"mode":"real"}`。公共设置接口只接收模式、时长档、默认时长、图片/文件限制、码率、分辨率和工作租约；模型凭据通过 profiles 管理。任何校验失败都回滚。

模型 PATCH 的 expected_version 比较模型自己的 version，DELETE 通过 query `?expected_version=2` 比较，成功为 204。省略密钥或 `{"secrets":{"api_key":""}}` 表示保留；`{"clear_secrets":["api_key"]}` 显式清除，同名新密钥与清除指令同时出现返回 422。

修改配置只影响新工作。已提交到供应商或提交结果不确定的执行保留原快照、原任务号和原凭据进行查询与下载，避免重复提交。首版公开 API 不会自动实现尚未注册的真实 voice/music 服务。

## 错误、健康与恢复

统一错误格式：

```json
{"error":{"code":"STATE_CONFLICT","message":"操作与当前状态、版本或幂等请求冲突","retryable":false,"request_id":"示例请求ID"}}
```

错误可选包含 task_id、generation_id、plan_version、fields、details；不返回输入值、供应商原始错误、SQL、堆栈或本地路径。X-Request-ID 与 error.request_id 对应。常见状态码：422 参数错误、404 不存在、409 状态/版本/幂等冲突、413 上传超限、503 执行依赖不可用、500 未知内部错误。

`GET /health/live` 只检查 HTTP 存活；`GET /health/ready` 检查连接、完整 schema 与迁移版本、FFmpeg/ffprobe、主密钥与有效配置，以及最近 30 秒内且未过期的单 Worker 租约。失败为 503，details.checks 只给脱敏布尔项。未启动 Worker 时 ready 应为 503，不代表 HTTP 无法提交任务。

停止服务时先停止接收新任务，再优雅停止 Worker；不要修改进行中任务的表记录来强制完成。重新启动使用相同数据库、文件根目录和主密钥，Worker 恢复过期租约。已有外部任务或不确定提交不会盲目重新 POST。

## 旧数据迁移与测试

旧数据迁移是显式部署操作。先停止旧 Worker 并等待有效租约结束，指定真实旧 home 和实际配置来源，先 dry-run：

```powershell
.venv/Scripts/python.exe -m video_agent.migration --source-home 'D:/old-video-agent' --config-path 'D:/old-video-agent/config.json' --dry-run
```

确认报告后，以 `--apply --backup-dir 'D:/video-agent-backup/new-run'` 代替 `--dry-run` 执行。备份目录不能覆盖同名内容；工具保留源 SQLite、JSON 和媒体。配置来源必须明确，不能默认把仓库 config.json 当成运行配置。只支持当前已知旧 schema；冲突整体回滚。本次开发不自动执行正式用户数据迁移。

隔离测试必须使用独立 `VIDEO_AGENT_TEST_DB_*`；测试拒绝正式库名及不符合 `test_*`/`*_test` 形式的库名。只对合成数据执行清理。HTTP 测试默认使用显式 SQLite 注入验证协议；真实 MySQL 验收另运行：

```powershell
.venv/Scripts/python.exe -m pytest tests/api --api-mysql -q
.venv/Scripts/python.exe -m pytest tests/mysql -q
```

不要并行运行会清理同一测试库的测试进程。端到端测试必须能访问 FFmpeg/ffprobe，检查真实可解码成片、帧数、尺寸与 Range 内容；不会用伪造 MP4 字节代替成片验收，不调用真实付费供应商。
