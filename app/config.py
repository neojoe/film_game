"""运行配置:从环境变量 / .env 读取。没有 API Key 时游戏仍可完整游玩(预写文本 + 预生成插画)。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
SCENE_DIR = STATIC_DIR / "assets" / "scenes"


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # 主模型 → 备用模型。免费模型会在高峰期被限流(HTTP 429),备用模型保证体验不断档。
    llm_models: tuple[str, ...] = ("glm-4.7-flash", "glm-4-flash-250414")
    image_model: str = "cogview-3-flash"
    llm_read_timeout: float = 12.0  # 单次读取(含首 token)超时
    narration_deadline: float = 40.0  # 一次旁白生成的总时限
    session_ttl_seconds: int = 4 * 3600

    @property
    def ai_enabled(self) -> bool:
        return bool(self.api_key)


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    primary = os.getenv("LLM_MODEL", "glm-4.7-flash").strip()
    fallbacks = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "glm-4-flash-250414").split(",") if m.strip()]
    models = tuple(dict.fromkeys([primary, *fallbacks]))  # 去重并保持顺序
    return Settings(
        api_key=os.getenv("ZHIPU_API_KEY", "").strip(),
        base_url=os.getenv("ZHIPU_BASE_URL", Settings.base_url).rstrip("/"),
        llm_models=models,
        image_model=os.getenv("IMAGE_MODEL", Settings.image_model).strip(),
    )
