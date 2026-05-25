"""structured_properties — 可插拔的结构化属性 extractor 注册机制。

新增结构化属性的步骤：
  1. 实现一个继承 BaseExtractor 的类，设置 PROPERTY_URN，实现 extract() 方法。
  2. 在 DEFAULT_EXTRACTORS 注册（或像 ExecuteShell 一样仅在血缘成功时由 collect_structured_properties_for_sync 追加）。
  3. 在 batch_sync / sync_job_lineage 调用 collect_structured_properties_for_sync（或等价逻辑）。

Execute Shell（blf.data.schedule.execute_shell）须在 GMS 预建为富文本属性，且仅在有表级血缘写入时 PATCH。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import List

from .logging_utils import get_logger
from .models import JobContext, StructuredPropertyValue

logger = get_logger("structured_properties")

# 结构化属性 URN 常量，修改属性定义只需改这里
URN_ETL_SCRIPT = "urn:li:structuredProperty:blf.data.warehouse.etl_script"
URN_SCHEDULE_URL = "urn:li:structuredProperty:blf.data.schedule.schedule_url"
URN_EXECUTE_SHELL = "urn:li:structuredProperty:blf.data.schedule.execute_shell"
URN_DATA_AVAILABILITY_FLAG = "urn:li:structuredProperty:blf.data.warehouse.data_availability_flag"

SCHEDULE_URL_TEMPLATE = "https://schedule.corp.bianlifeng.com/job/{job}"

# 文件扩展名 → Markdown 代码块语言标记
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".job": "sql",
    ".hql": "sql",
    ".sh": "bash",
    ".sql": "sql",
}


def _wrap_etl_script(content: str, file_name: str) -> str:
    """将脚本内容包装为带语法标记的 Markdown 代码块。

    DataHub 富文本字段支持 Markdown 渲染，代码块语言标记决定语法高亮效果：
      .py  → python
      .job → sql
      其他 → text（等宽，无高亮）
    """
    ext = os.path.splitext(file_name)[-1].lower()
    lang = _EXT_TO_LANG.get(ext, "text")
    return f"```{lang}\n{content}\n```"


def _wrap_shell_command(content: str) -> str:
    """将 DMP shell_command 全文包装为 shell 语法高亮的 Markdown 代码块。"""
    return f"```shell\n{content}\n```"


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class BaseExtractor(ABC):
    """所有 structured property extractor 的基类。"""

    PROPERTY_URN: str = ""

    @abstractmethod
    def extract(self, context: JobContext) -> List[StructuredPropertyValue]:
        """从 JobContext 中提取属性值。返回空列表表示此次不写入。"""
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(urn={self.PROPERTY_URN!r})"


# ---------------------------------------------------------------------------
# 内置 extractor
# ---------------------------------------------------------------------------


class EtlScriptExtractor(BaseExtractor):
    """写入 ETL 脚本全文，按文件类型包装为对应语法的 Markdown 代码块。"""

    PROPERTY_URN = URN_ETL_SCRIPT

    def extract(self, context: JobContext) -> List[StructuredPropertyValue]:
        if context.runtime is None:
            logger.warning("EtlScriptExtractor: runtime 为 None，跳过")
            return []
        content = context.runtime.etl_content
        file_name = context.runtime.job_file_name
        if not content:
            logger.warning("EtlScriptExtractor: etl_content 为空，跳过")
            return []
        wrapped = _wrap_etl_script(content, file_name)
        logger.debug(
            "EtlScriptExtractor: file=%s lang=%s chars=%d",
            file_name,
            _EXT_TO_LANG.get(os.path.splitext(file_name)[-1].lower(), "text"),
            len(wrapped),
        )
        return [StructuredPropertyValue(property_urn=self.PROPERTY_URN, string_value=wrapped)]


class ScheduleUrlExtractor(BaseExtractor):
    """写入调度系统链接，格式为 https://schedule.corp.bianlifeng.com/job/<job_display_name>。"""

    PROPERTY_URN = URN_SCHEDULE_URL

    def extract(self, context: JobContext) -> List[StructuredPropertyValue]:
        job = context.metadata.job_display_name
        if not job:
            logger.warning("ScheduleUrlExtractor: job_display_name 为空，跳过")
            return []
        url = SCHEDULE_URL_TEMPLATE.format(job=job)
        logger.debug("ScheduleUrlExtractor: url=%s", url)
        return [StructuredPropertyValue(property_urn=self.PROPERTY_URN, string_value=url)]


class ExecuteShellExtractor(BaseExtractor):
    """写入 DMP shell_command 全文（与 Jenkins Execute shell 同源），富文本 shell 代码块。

    不加入 DEFAULT_EXTRACTORS；仅在有表级血缘且 write_upstream_lineage 时由 collect_structured_properties_for_sync 调用。
    """

    PROPERTY_URN = URN_EXECUTE_SHELL

    def extract(self, context: JobContext) -> List[StructuredPropertyValue]:
        content = (context.metadata.shell_command or "").strip()
        if not content:
            logger.warning("ExecuteShellExtractor: shell_command 为空，跳过")
            return []
        wrapped = _wrap_shell_command(content)
        logger.debug("ExecuteShellExtractor: chars=%d", len(wrapped))
        return [StructuredPropertyValue(property_urn=self.PROPERTY_URN, string_value=wrapped)]


# ---------------------------------------------------------------------------
# 注册表与运行入口
# ---------------------------------------------------------------------------

# 新增 extractor 在此列表追加，不需要改动其他文件
DEFAULT_EXTRACTORS: List[BaseExtractor] = [
    EtlScriptExtractor(),
    ScheduleUrlExtractor(),
]


def run_all_extractors(
    context: JobContext,
    extractors: List[BaseExtractor] | None = None,
) -> List[StructuredPropertyValue]:
    """运行所有已注册的 extractor，汇总结构化属性写入值。

    参数 extractors 为 None 时使用 DEFAULT_EXTRACTORS。
    单个 extractor 抛出异常时记录告警并继续，不中断整体流程。
    """
    active = extractors if extractors is not None else DEFAULT_EXTRACTORS
    results: List[StructuredPropertyValue] = []
    for ext in active:
        try:
            vals = ext.extract(context)
            results.extend(vals)
            logger.debug("extractor %s 产出 %d 个属性值", ext, len(vals))
        except Exception as exc:
            msg = f"extractor {ext} 执行异常: {exc}"
            logger.warning(msg)
            context.warnings.append(msg)
    return results


def collect_structured_properties_for_sync(
    context: JobContext,
    *,
    table_lineages: list,
    write_upstream_lineage: bool,
) -> List[StructuredPropertyValue]:
    """收集本次同步要 PATCH 的结构化属性。

    Execute Shell 仅在 table_lineages 非空且 write_upstream_lineage 为真时追加，
    与 upstreamLineage 写入门控一致。
    """
    props = run_all_extractors(context, DEFAULT_EXTRACTORS)
    if table_lineages and write_upstream_lineage:
        shell_vals = ExecuteShellExtractor().extract(context)
        if shell_vals:
            props.extend(shell_vals)
            logger.debug(
                "ExecuteShell 已加入 PATCH（lineage 已解析）job=%s",
                context.metadata.job_display_name,
            )
    return props
