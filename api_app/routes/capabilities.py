from fastapi import APIRouter, Depends

from api_app.dependencies import Settings, require_business
from api_app.schemas import CapabilitiesView, ERROR_RESPONSES
from video_agent.config import ASPECT_RATIOS
from video_agent.factory import adapter_type_metadata


router = APIRouter(prefix="/api/v1", tags=["系统能力"], dependencies=[Depends(require_business)], responses=ERROR_RESPONSES)


@router.get("/capabilities", response_model=CapabilitiesView, operation_id="getCapabilities", summary="查询比例、时长、上传限制与适配器能力", description="能力来自注册的适配器元数据；不返回配置凭据或当前服务地址。")
def get_capabilities(service: Settings):
    settings = service.read_settings()
    return CapabilitiesView(aspect_ratios=list(ASPECT_RATIOS), duration_tiers=settings.duration_tiers, default_duration=settings.default_duration, max_images=settings.max_images, max_file_bytes=settings.max_file_bytes, image_media_types=["image/jpeg", "image/png", "image/webp"], audio_media_types=["audio/mpeg", "audio/wav", "audio/mp4"], adapters=adapter_type_metadata())
