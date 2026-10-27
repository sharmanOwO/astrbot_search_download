"""通过soutubot 实现反向搜图和通过 nhentai 下载漫画的工具"""

import asyncio
import math
import os
import shutil
import tempfile
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .download_comic import DEFAULT_PDF_PASSWORD, DownloadError, download_pdf
from .plugin_utils import resolve_first_image, schedule_cleanup
from .soutubot_search import SearchError, format_summary, search_image


def _positive_float(value: str | None, default: float) -> float:
    try:
        parsed = float(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _non_negative_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


@register(
    "astrbot_soutu_download",
    "sharmanOwO",
    "提供 soutubot 反向搜图和 nhentai 漫画 PDF 下载工具。",
    "1.1.0",
)
class AstrbotSoutuDownloadPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.timeout = _positive_float(os.getenv("ASTRBOT_SOUTU_TIMEOUT"), 60.0)
        self.result_limit = _non_negative_int(
            os.getenv("ASTRBOT_SOUTU_RESULT_LIMIT"),
            10,
        )
        self.search_settings: dict[tuple[str, str], dict[str, bool | int]] = {}
        # 会话隔离设定。字典中的元素会形如("群A", "用户1"): {"detail": True, "limit": 3}
        self.download_root = Path(
            os.getenv(
                "ASTRBOT_NHENTAI_DOWNLOAD_DIR",
                "/AstrBot/data/plugin_data/astrbot_soutu_download/downloads",
            )
        )
        self.pdf_password = (
            os.getenv("NHENTAI_PDF_PASSWORD", DEFAULT_PDF_PASSWORD)
            or DEFAULT_PDF_PASSWORD
        )

    def _settings_for(self, event: AstrMessageEvent) -> dict[str, bool | int]:
        """Keep presentation preferences isolated by conversation and sender."""
        key = (event.unified_msg_origin, event.get_sender_id())
        return self.search_settings.setdefault(key, {})
    # setdefault：
    # 如果字典里有这个键，就返回它对应的值
    # 如果没有这个键返回默认值
    # 这里默认值就是空字典
    @filter.command("detail")
    async def detail(self, event: AstrMessageEvent, enabled: str = "true"):
        """切换搜图详细模式：/detail [1/0/true/false]，省略参数即开启。"""
        value = enabled.strip().lower()
        if value not in {"1", "0", "true", "false"}:
            yield event.plain_result(
                "用法：/detail [1/0/true/false]；不带参数表示开启详细模式喵"
            )
        else:
            active = value in {"1", "true"}
            self._settings_for(event)["detail"] = active
            # 用户+会话隔离的情况下，这相当于将
            # ("群A", "用户1")的 detail 键设置为 true
            mode = "详细" if active else "简洁"
            yield event.plain_result(f"现在你在当前会话中的搜图设置为：{mode}模式喵~")
        event.stop_event()

    @filter.command("page")
    async def page(self, event: AstrMessageEvent, n: int):
        """设置搜图展示条数：/page n，只展示本次结果中匹配度最高的前 n 条。"""
        if n <= 0:
            yield event.plain_result("用法：/page n，展示匹配度最高的前 n 条喵")
        else:
            self._settings_for(event)["limit"] = n
            yield event.plain_result(
                f"你在当前会话中的搜图结果将会展示前 {n} 条喵~"
            )
        event.stop_event()

    @filter.llm_tool(name="soutubot_search_tool")
    async def soutubot_search_tool(
        self,
        event: AstrMessageEvent,
        strict: bool = False,
        result_limit: int = -1,
    ):
        """使用 soutubot 对当前消息或引用消息中的图片进行反向搜图。

        只有当用户明确要求搜索、识图或反向搜索当前图片时才调用此工具。
        图片可以直接附在当前消息中，也可以位于用户引用的消息中。

        Args:
            strict(boolean): 是否使用更严格的相似度搜索模式。
            result_limit(number): 最多展示多少条结果；用户设置过 /page 时优先使用该设置。
        """
        image_path, temporary = await resolve_first_image(event)
        if image_path is None:
            yield event.plain_result(
                "需要引用或包含一张图片喵"
            )
            return

        settings = self._settings_for(event)
        if "limit" in settings:
            limit = int(settings["limit"])
        elif (
            isinstance(result_limit, bool)
            or not isinstance(result_limit, (int, float))
            or not math.isfinite(result_limit)
            or result_limit < 0
        ):
            limit = self.result_limit
        else:
            limit = max(0, int(result_limit))
        detail = bool(settings.get("detail", False))
        # 如果未发送 /detail
        # 那么用户对应字典中没有detail键，
        # get使用默认值False，也就是简洁模式

        try:
            payload = await asyncio.to_thread(
                search_image,
                image_path,
                bool(strict),
                self.timeout,
            )
            yield event.plain_result(format_summary(payload, limit, detail=detail))
        except (OSError, SearchError, ValueError) as exc:
            logger.warning("soutubot 搜图失败: %s", exc)
            yield event.plain_result(f"搜图失败：{exc}")
        finally:
            if temporary:
                schedule_cleanup((image_path,))

    @filter.llm_tool(name="nhentai_download_tool")
    async def nhentai_download_tool(
        self,
        event: AstrMessageEvent,
        gallery_id: str = "",
    ):
        """从 nhentai 下载指定编号的漫画并生成 PDF 文件发送给用户。

        用户提出下载某个 nhentai 漫画编号（也可以提供完整的 nhentai 主页面 URL）
        时调用此工具。不要把普通聊天中的数字误当成漫画编号。

        Args:
            gallery_id(string): nhentai 漫画编号，例如 123456，或 https://nhentai.net/g/123456/。
        """
        if not str(gallery_id).strip():
            yield event.plain_result("请提供要下载的 nhentai 车牌号喵")
            return

        try:
            self.download_root.mkdir(parents=True, exist_ok=True)
            self.download_root.chmod(0o755)
            output_dir = Path(
                tempfile.mkdtemp(
                    prefix="astrbot-nhentai-",
                    dir=str(self.download_root),
                )
            )
            # NapCat runs in another container and must be able to traverse this
            # directory through the shared /AstrBot/data bind mount.
            output_dir.chmod(0o755)
        except OSError as exc:
            logger.warning("无法创建 nhentai 共享下载目录: %s", exc)
            yield event.plain_result(f"无法创建 nhentai 下载目录：{exc}")
            return

        try:
            pdf_path = await asyncio.to_thread(
                download_pdf,
                str(gallery_id),
                output_dir,
                self.timeout,
                pdf_password=self.pdf_password,
            )
            pdf_path.chmod(0o644)
            chain = [
                Comp.Plain(f" {gallery_id} 下载完成喵\n密码：{self.pdf_password}喵"),
                Comp.File(name=pdf_path.name, file=str(pdf_path)),
            ]
            yield event.chain_result(chain)
            schedule_cleanup((output_dir,), delay=3600)
        except (DownloadError, OSError, ValueError) as exc:
            logger.warning("nhentai 下载失败: %s", exc)
            shutil.rmtree(output_dir, ignore_errors=True)
            yield event.plain_result(f"nhentai 下载失败：{exc}")

    async def terminate(self):
        """插件卸载时无需额外清理。"""
