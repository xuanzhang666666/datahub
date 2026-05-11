# scripts

- **`gms-es/`** — 调 GMS OpenAPI / GraphQL、查 `datasetindex_v2` 的请求体与示例（含 Hive `browsePathV2` 修复相关 JSON；`graphql_scroll_schema_counts.example.json` 为按库名前缀的 `scrollAcrossEntities` 样例）。

### 导出整张表的元数据给 LLM（`default.dw_order_v1`）

1. 表 URN（BLF Hive PROD）：`urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_order_v1,PROD)`  
2. GraphQL 文件：`gms-es/gms_query_dataset_for_llm.graphql`，变量：`gms-es/gms_variables_dw_order_v1.json`（改 `urn` 可换表）。  
3. **DataHub CLI**（已 `datahub init`）：  
   `datahub graphql --query-file xander/scripts/gms-es/gms_query_dataset_for_llm.graphql --variables-file xander/scripts/gms-es/gms_variables_dw_order_v1.json --format json`  
4. **curl 直连 GMS**（无 CLI 时）：先 `python3 xander/scripts/gms-es/build_gms_body_dataset_for_llm.py` 生成 `gms_body_dataset_for_llm_dw_order_v1.json`，再：  
   `curl -sS -H 'Content-Type: application/json' -H "Authorization: Bearer $DATAHUB_GMS_TOKEN" "$DATAHUB_GMS_URL/api/graphql" --data-binary @xander/scripts/gms-es/gms_body_dataset_for_llm_dw_order_v1.json`  
   无鉴权时去掉 `Authorization` 头。将返回的 JSON 整段提供给 LLM 即可（内含 schema、血缘摘要、browse、owner 等）。
