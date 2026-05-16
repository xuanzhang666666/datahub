# job_info_sync_datahub — 测试与发布门禁

## 运行方式

在仓库内（**`xander/python` 为当前目录**）：

```bash
sh scripts/run_job_info_sync_datahub_tests.sh
```

或：

```bash
cd xander/python
export PYTHONPATH=.
pytest job_info_sync_datahub/tests/test_runtime_parser.py \
  job_info_sync_datahub/tests/test_manual_upstream_lineage.py \
  job_info_sync_datahub/tests/test_two_stage_tmp_lineage_contract.py -q
```

### 两段 SQL + `tmp_*` 中间表（LLM 契约）

先 `CREATE`/`INSERT` 临时表（如 `default.tmp_mid_*`）、再写正式分层表时，下游血缘写入前会做 **fqtn 校验**：`tmp_*` 不是合法层级前缀，会被整段丢弃或从 upstreams 剥除。DeepSeek 的 **`SYSTEM_PROMPT`**（[`lineage_llm_compare.py`](lineage_llm_compare.py)）要求模型：**target 仅最终落表**；**upstreams 不含 `tmp_*`**，并把前段 SQL 读过的持久化表与后段合并（跨段折叠）。理想 JSON 形态见单测 [`tests/test_two_stage_tmp_lineage_contract.py`](tests/test_two_stage_tmp_lineage_contract.py)。

### 默认门禁 vs 全量

| 方式 | 说明 |
|------|------|
| `sh scripts/run_job_info_sync_datahub_tests.sh` | **默认**：仅 `runtime_parser` + `manual_upstream_lineage` 单测，**不要求 trino**，通过后再上传 neo4j2。 |
| `FULL_TESTS=1 sh scripts/run_job_info_sync_datahub_tests.sh` | 跑 **全部** `tests/`，且当前 Python 必须能 `import trino`（否则脚本退出 1）。 |

## 依赖

- **pytest**（`pip install pytest`）。
- **acryl-datahub**（可选）：未安装时 `manual_upstream_lineage` 里依赖 SDK 的合并用例会 **skip**；安装后可跑全部分支。
- **trino**（仅 `FULL_TESTS=1` 全量时需要）。

## 发布流程（neo4j2 / FTP）

1. 修改 `job_info_sync_datahub/` 或相关 `scripts/`。  
2. **`sh scripts/run_job_info_sync_datahub_tests.sh` 必须通过。**  
3. 再打 `job_info_sync_datahub.tar.gz` 并 `put2` / `get2` 同步（见仓库 `xander/README.md`）。  
4. 若需验证与 DMP/Hive 强相关的用例，在具备 trino 的环境执行 **`FULL_TESTS=1`** 后再发版。
