from fastapi import APIRouter, Depends, Query, Response

from api_app.dependencies import Agent, IdempotencyKey, require_business
from api_app.errors import safe_artifact, safe_view
from api_app.schemas import ConfirmRequest, ERROR_RESPONSES, StrictCreateTaskRequest, StrictInputPatch, StrictPlanChange, StrictRegenerateRequest
from video_agent.models import ArtifactView, GenerationView, PlanView, TaskPage, TaskView


router = APIRouter(prefix="/api/v1", tags=["任务与方案"], dependencies=[Depends(require_business)], responses=ERROR_RESPONSES)


@router.post("/tasks", status_code=201, response_model=TaskView, operation_id="createTask", summary="创建视频任务", description="先上传素材，再提交其 asset_id。立即返回已落库任务，不等待规划；轮询 GET task，取得方案后再确认生成。")
def create_task(body: StrictCreateTaskRequest, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.create_task(body, idempotency_key=key))


@router.get("/tasks", response_model=TaskPage, operation_id="listTasks", summary="分页查询任务", description="按 updated_at DESC、task_id DESC 排序；三个筛选字段精确匹配，external_user_id 不作为权限凭证。")
def list_tasks(agent: Agent, limit: int = Query(20, ge=1, le=100, description="每页数量，最大 100。"), offset: int = Query(0, ge=0, description="跳过的记录数。"), status: str | None = Query(None, description="任务状态，精确匹配。"), external_user_id: str | None = Query(None), external_request_id: str | None = Query(None)):
    return agent.list_tasks_page(limit=limit, offset=offset, status=status, external_user_id=external_user_id, external_request_id=external_request_id)


@router.get("/tasks/{task_id}", response_model=TaskView, operation_id="getTask", summary="查询任务状态与资料")
def get_task(task_id: str, agent: Agent):
    return safe_view(agent.get_task(task_id))


def _queued(view) -> bool:
    return view.status in {"pending", "planning", "generating"}


@router.patch("/tasks/{task_id}/inputs", response_model=TaskView, operation_id="updateTaskInputs", summary="补充任务资料", description="立即更新返回 200；资料完整且入队后返回 202。拒绝未定义字段。", responses={202: {"model": TaskView, "description": "资料已保存并排队规划。"}})
def update_inputs(task_id: str, body: StrictInputPatch, response: Response, agent: Agent, key: IdempotencyKey = None):
    view = agent.submit_inputs(task_id, body, idempotency_key=key)
    response.status_code = 202 if _queued(view) else 200
    return safe_view(view)


@router.get("/tasks/{task_id}/plans/{plan_version}", response_model=PlanView, operation_id="getPlanVersion", summary="读取方案版本")
def get_plan(task_id: str, plan_version: int, agent: Agent):
    return agent.get_plan(task_id, plan_version)


@router.post("/tasks/{task_id}/plan-revisions", response_model=TaskView, operation_id="reviseTaskPlan", summary="修改方案", description="结构化 patch 立即产生新版本并返回 200；自然语言 instruction 排队规划并返回 202。", responses={202: {"model": TaskView, "description": "自然语言修改已排队。"}})
def revise_plan(task_id: str, body: StrictPlanChange, response: Response, agent: Agent, key: IdempotencyKey = None):
    view = agent.revise_plan(task_id, body, idempotency_key=key)
    response.status_code = 202 if _queued(view) else 200
    return safe_view(view)


@router.post("/tasks/{task_id}/discard-revision", response_model=TaskView, operation_id="discardTaskRevision", summary="放弃未确认的修改")
def discard_revision(task_id: str, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.discard_revision(task_id, idempotency_key=key))


@router.post("/tasks/{task_id}/confirm", status_code=202, response_model=TaskView, operation_id="confirmTaskPlan", summary="确认方案并排队生成", description="仅能确认当前方案头。返回 generation_id，轮询 GET generations 获取阶段与进度。")
def confirm(task_id: str, body: ConfirmRequest, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.confirm(task_id, body.plan_version, idempotency_key=key))


@router.post("/tasks/{task_id}/retry-planning", status_code=202, response_model=TaskView, operation_id="retryTaskPlanning", summary="重试失败规划")
def retry_planning(task_id: str, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.retry_planning(task_id, idempotency_key=key))


@router.post("/tasks/{task_id}/regenerations", status_code=202, response_model=TaskView, operation_id="regenerateTask", summary="整条或指定镜头重新生成", description="kind=shots 时提供 artifact_id 和 shot_ids；复用原成品对应方案。")
def regenerate(task_id: str, body: StrictRegenerateRequest, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.regenerate(task_id, body, idempotency_key=key))


@router.get("/generations/{generation_id}", response_model=GenerationView, operation_id="getGeneration", summary="查询生成阶段和进度", tags=["生成与成品"])
def get_generation(generation_id: str, agent: Agent):
    return safe_view(agent.get_generation(generation_id))


@router.post("/generations/{generation_id}/retry", status_code=202, response_model=TaskView, operation_id="retryGeneration", summary="重试失败生成", description="存在外部任务号或提交结果不确定时，保留原工作快照并安全恢复。", tags=["生成与成品"])
def retry_generation(generation_id: str, agent: Agent, key: IdempotencyKey = None):
    return safe_view(agent.retry_generation(generation_id, idempotency_key=key))


@router.get("/tasks/{task_id}/artifacts", response_model=list[ArtifactView], operation_id="listTaskArtifacts", summary="查询任务成片版本", tags=["生成与成品"])
def list_artifacts(task_id: str, agent: Agent):
    return [safe_artifact(view) for view in agent.list_artifacts(task_id)]
