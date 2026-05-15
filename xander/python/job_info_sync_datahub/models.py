"""数据模型：描述调度作业血缘解析全流程中各阶段的结构化数据。

所有模型使用 dataclass，不依赖第三方库，便于序列化到 JSON 产物。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ParseConfidence(str, Enum):
    """血缘解析置信度。"""

    HIGH = "HIGH"  # AST 静态解析，结果明确
    PARTIAL = "PARTIAL"  # 部分字段无法确认（如子查询、*）
    UNRESOLVED = "UNRESOLVED"  # 完全无法静态解析（动态 SQL、变量未展开）


class ParseStatus(str, Enum):
    """单个 SQL block 的解析状态。"""

    OK = "OK"
    SQL_PARSE_FAILED = "SQL_PARSE_FAILED"  # sqlglot 无法解析语法
    SQL_PARSE_PARTIAL = "SQL_PARSE_PARTIAL"  # 部分成功
    SKIPPED = "SKIPPED"  # 非 DML/DDL block，主动跳过


@dataclass
class JobMetadata:
    """从 DMP 调度表读取的原始作业信息。"""

    job_display_name: str
    job_name: str
    shell_command: str
    upstream_jobs: List[str] = field(default_factory=list)
    dt: str = ""  # 元数据分区日期


@dataclass
class RuntimeContext:
    """从 shell_command 解析出的运行时上下文。"""

    job_display_name: str
    gitlab_name: str  # e.g. "analysis-jobs"
    project_path: str  # e.g. "data/analysis-jobs"；空串表示仅 localfolder（无 GitLab 映射）
    job_path: str  # e.g. "pdw_opc_flag/pdw_opc_flag_contact"
    job_type: str  # "job" | "python"
    job_file_name: str  # e.g. "pdw_opc_flag_pdw_opc_flag_contact.job"
    gitlab_file_path: str  # 实际在 GitLab 中找到的仓库路径
    etl_content: str  # 脚本原文
    runtime_params: Dict[str, str] = field(default_factory=dict)  # --key=value 参数


@dataclass
class TableRef:
    """表引用：库名 + 表名。"""

    db: str
    table: str

    @property
    def full_name(self) -> str:
        return f"{self.db}.{self.table}"

    def __hash__(self) -> int:
        return hash((self.db.lower(), self.table.lower()))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TableRef):
            return NotImplemented
        return self.db.lower() == other.db.lower() and self.table.lower() == other.table.lower()


@dataclass
class FieldMapping:
    """一条字段级血缘映射。"""

    target_field: str
    source_table: Optional[str]  # None 表示来源不确定
    source_field: Optional[str]  # None 表示表达式复杂或不确定
    expression: str  # 原始 SQL 表达式片段
    confidence: ParseConfidence = ParseConfidence.HIGH


@dataclass
class SqlBlock:
    """从 ETL 脚本中提取的单个 SQL 语句块。"""

    index: int  # 在脚本中的顺序
    raw_sql: str  # 变量展开后的 SQL 文本
    status: ParseStatus = ParseStatus.OK
    error_detail: str = ""

    # 解析结果
    target_tables: List[TableRef] = field(default_factory=list)
    upstream_tables: List[TableRef] = field(default_factory=list)
    field_mappings: List[FieldMapping] = field(default_factory=list)
    confidence: ParseConfidence = ParseConfidence.HIGH


@dataclass
class TableLineage:
    """表级血缘：一个目标表及其所有上游表。"""

    target: TableRef
    upstreams: List[TableRef] = field(default_factory=list)
    # 哪些 sql_block 贡献了这条血缘
    source_block_indices: List[int] = field(default_factory=list)


@dataclass
class FieldLineage:
    """字段级血缘：目标表的一个字段及其来源。"""

    target_table: TableRef
    target_field: str
    mappings: List[FieldMapping] = field(default_factory=list)
    confidence: ParseConfidence = ParseConfidence.HIGH


@dataclass
class StructuredPropertyValue:
    """单个结构化属性的写入值。"""

    property_urn: str
    string_value: str  # DataHub 结构化属性目前只写 string 类型


@dataclass
class JobContext:
    """主流程上下文：持有解析全阶段产物，传递给 extractor 和 writer。"""

    metadata: JobMetadata
    runtime: Optional[RuntimeContext] = None
    sql_blocks: List[SqlBlock] = field(default_factory=list)
    table_lineages: List[TableLineage] = field(default_factory=list)
    field_lineages: List[FieldLineage] = field(default_factory=list)
    # extractor 汇总结果
    structured_properties: List[StructuredPropertyValue] = field(default_factory=list)
    # 解析过程中的告警/错误摘要，供最终日志输出
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
