# LLM 配置与 Jenkins 切换说明

本文档用于排查 `job_info_sync_datahub` 相关脚本的 LLM 配置。

## 当前配置来源

服务器默认配置文件：

```bash
/data/datahub/scripts/lineage.env
```

该文件保存长期默认值，例如：

```bash
BLF_ACTIVE_LLM=blf
BLF_LLM_BASE_URL=http://token-pool.vip.blibee.com/v1
BLF_LLM_MODEL=gpt-5.5
BLF_LLM_SSL_VERIFY=0

DEEPSEEK_OPENAI_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-pro
```

`BLF_LLM_API_KEY` 和 `DEEPSEEK_API_KEY` 也在该文件中配置，但排查时不要在日志、文档或截图中暴露密钥值。

## Jenkins 推荐参数

Jenkins 任务中推荐只配置这两个参数：

```bash
LLM_PROVIDER=blf
LLM_MODEL=gpt-5.5
```

`LLM_PROVIDER` 可选：

```text
blf
deepseek
```

`LLM_MODEL` 填具体模型名。

## 参数优先级

代码中的优先级如下：

```text
LLM_PROVIDER > BLF_ACTIVE_LLM
LLM_MODEL    > BLF_LLM_MODEL / DEEPSEEK_MODEL
```

也就是说，即使 `/data/datahub/scripts/lineage.env` 里写了：

```bash
BLF_ACTIVE_LLM=blf
BLF_LLM_MODEL=gpt-5.5
```

Jenkins 任务中传入：

```bash
LLM_PROVIDER=blf
LLM_MODEL=claude-sonnet-4-5-20250929
```

实际会使用：

```text
provider=blf
model=claude-sonnet-4-5-20250929
```

## BLF Token Pool 模型切换

BLF token-pool 走 OpenAI-compatible 接口：

```bash
BLF_LLM_BASE_URL=http://token-pool.vip.blibee.com/v1
```

切换 BLF 支持的模型时，只需要改 Jenkins 参数 `LLM_MODEL`。

示例：

```bash
LLM_PROVIDER=blf
LLM_MODEL=gpt-5.5
```

```bash
LLM_PROVIDER=blf
LLM_MODEL=gpt-5.4-mini
```

```bash
LLM_PROVIDER=blf
LLM_MODEL=claude-sonnet-4-5-20250929
```

```bash
LLM_PROVIDER=blf
LLM_MODEL=qwen3.7-max
```

## DeepSeek 切换

如果要切到 DeepSeek：

```bash
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-v4-pro
```

或：

```bash
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-v4-flash
```

DeepSeek base URL 来自：

```bash
DEEPSEEK_OPENAI_BASE_URL=https://api.deepseek.com/v1
```

## 常用模型名

BLF token-pool 页面当前可见模型包括：

```text
gpt-5.4-mini
gpt-5.2
gpt-5.3-codex
gpt-5.3-codex-spark
gpt-5.4
gpt-5.5

claude-opus-4-5-20251101
claude-3-7-sonnet-20250219
claude-haiku-4-5-20251001
claude-opus-4-6
claude-opus-4-7
claude-opus-4-20250514
claude-sonnet-4-6
claude-sonnet-4-20250514
claude-sonnet-4-5-20250929
claude-opus-4-1-20250805
claude-3-5-haiku-20241022

kimi-k2.6
qwen3.7-max
glm-5.1
deepseek-v4-pro
deepseek-v4-flash
```

如果 token-pool 页面新增模型，Jenkins 里直接把新模型名填到 `LLM_MODEL` 即可。

## 排查命令

在 neo4j2 上检查实际导入的代码文件：

```bash
cd /data/datahub/scripts
PYTHONPATH=/data/datahub/scripts /opt/anaconda3/bin/python -c 'import job_info_sync_datahub.llm_client as m; print(m.__file__)'
```

检查 Jenkins 覆盖是否生效：

```bash
cd /data/datahub/scripts
env \
  BLF_ACTIVE_LLM=blf \
  BLF_LLM_API_KEY=dummy \
  BLF_LLM_MODEL=gpt-5.5 \
  LLM_PROVIDER=blf \
  LLM_MODEL=claude-sonnet-4-5-20250929 \
  PYTHONPATH=/data/datahub/scripts \
  /opt/anaconda3/bin/python -c 'from job_info_sync_datahub.llm_client import get_llm_config; print(get_llm_config())'
```

期望输出中应看到：

```text
provider='blf'
model='claude-sonnet-4-5-20250929'
```

如果输出仍然是 `gpt-5.5`，优先检查：

```bash
cd /data/datahub/scripts
PYTHONPATH=/data/datahub/scripts /opt/anaconda3/bin/python -c 'import job_info_sync_datahub.llm_client as m; print(m.__file__)'
```

确认 Python 没有导入到旧路径，例如 `/root/job_info_sync_datahub/llm_client.py`。

## 相关脚本

常见会调用 LLM 的脚本：

```text
run_batch_lineage_sync.sh
run_batch_lineage_sync_job_list.sh
run_batch_table_lineage_sync_from_datasets.sh
run_batch_table_documentation_from_datasets.sh
run_batch_table_documentation_all_from_datasets.sh
run_field_lineage_export_to_excel.sh
```

这些脚本通常会加载：

```bash
/data/datahub/scripts/lineage.env
```

然后再执行 Python 模块。Jenkins 参数 `LLM_PROVIDER`、`LLM_MODEL` 是用于任务级临时覆盖默认模型的推荐入口。
