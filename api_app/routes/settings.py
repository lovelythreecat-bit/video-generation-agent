from fastapi import APIRouter, Depends, Query, Response
from pydantic import ConfigDict, Field

from api_app.dependencies import IdempotencyKey, Settings, require_admin
from api_app.schemas import AdapterTypeView, ERROR_RESPONSES, PublicSettingsPatch
from video_agent.config import SettingsPatch
from video_agent.config_store import BindingsPatch, BindingsView, ModelProfileCreate, ModelProfileUpdate, ProfilePage, PublicModelProfile, SettingsView
from video_agent.factory import adapter_type_metadata


class VersionedBindingsPatch(BindingsPatch):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, description="GET bindings 返回的 configuration_revision；过期返回 409。")


router = APIRouter(prefix="/api/v1/admin", tags=["配置管理（管理员）"], dependencies=[Depends(require_admin)], responses=ERROR_RESPONSES)


@router.get("/adapter-types", response_model=list[AdapterTypeView], operation_id="listAdapterTypes", summary="查询适配器角色、参数和能力", description="仅返回注册元数据，不返回当前配置、地址或凭据。")
def adapter_types():
    return adapter_type_metadata()


@router.get("/model-profiles", response_model=ProfilePage, operation_id="listModelProfiles", summary="分页查询模型配置")
def list_profiles(service: Settings, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return service.list_profiles(limit=limit, offset=offset)


@router.post("/model-profiles", status_code=201, response_model=PublicModelProfile, operation_id="createModelProfile", summary="创建模型配置", description="secrets 只写不读；读取仅返回 has_api_key 与 secret_presence。配置名称可以重复。支持 Idempotency-Key。")
def create_profile(body: ModelProfileCreate, service: Settings, key: IdempotencyKey = None):
    return service.create_profile(body, idempotency_key=key)


@router.get("/model-profiles/{profile_id}", response_model=PublicModelProfile, operation_id="getModelProfile", summary="读取脱敏模型配置")
def get_profile(profile_id: str, service: Settings):
    return service.get_profile(profile_id)


@router.patch("/model-profiles/{profile_id}", response_model=PublicModelProfile, operation_id="updateModelProfile", summary="更新模型配置", description="必须提供 expected_version。省略秘密或传空字符串表示保留；clear_secrets 显式清除。绑定中的配置不可停用。")
def update_profile(profile_id: str, body: ModelProfileUpdate, service: Settings):
    return service.update_profile(profile_id, body)


@router.delete("/model-profiles/{profile_id}", status_code=204, operation_id="deleteModelProfile", summary="软删除模型配置", description="绑定中的配置不能删除；历史快照继续可恢复。")
def delete_profile(profile_id: str, service: Settings, expected_version: int = Query(..., ge=1, description="模型配置自己的 version。")):
    service.delete_profile(profile_id, expected_version=expected_version)
    return Response(status_code=204)


@router.get("/adapter-bindings", response_model=BindingsView, operation_id="getAdapterBindings", summary="读取当前角色绑定与配置修订")
def get_bindings(service: Settings):
    return service.read_bindings()


@router.patch("/adapter-bindings", response_model=BindingsView, operation_id="updateAdapterBindings", summary="原子更新角色绑定", description="全部角色一起校验，任何错误都不保存。停用配置或不兼容的角色不能绑定。")
def update_bindings(body: VersionedBindingsPatch, service: Settings):
    patch = BindingsPatch.model_validate(body.model_dump(exclude={"expected_version"}, exclude_unset=True))
    return service.set_bindings(patch, expected_version=body.expected_version)


@router.get("/settings", response_model=SettingsView, operation_id="getPublicSettings", summary="读取公共设置与配置修订")
def get_settings(service: Settings):
    return service.read_settings()


@router.patch("/settings", response_model=SettingsView, operation_id="updatePublicSettings", summary="更新公共设置", description="expected_version 使用 configuration_revision。新工作采用新配置，已提交工作继续原快照；模式或参数验证失败时事务回滚。")
def update_settings(body: PublicSettingsPatch, service: Settings):
    patch = SettingsPatch.model_validate(body.model_dump(exclude={"expected_version"}, exclude_unset=True))
    service.save_settings(patch, expected_version=body.expected_version)
    return service.read_settings()
