-- 从元信息维表导出 DataHub 表名列表（每行一个 db.table），供
--   xander/run/export_hive_table_list_from_dim_meta.py / .sh 使用。
--
-- 分区 dt 请按实际替换，或通过脚本环境变量 HIVE_META_DT 传入。
--
-- 等价逻辑（Trino；连接 catalog 为 hive 时 default.xxx 即 hive.default.xxx）:
SELECT DISTINCT
    trim(cast(db_name AS varchar)) || '.' || trim(cast(table_name AS varchar)) AS fqtn
FROM default.dim_dc_hive_meta_info_di
WHERE dt = '${HIVE_META_DT}'
  AND is_hive_table = '1'
  AND trim(cast(db_name AS varchar)) <> ''
  AND trim(cast(table_name AS varchar)) <> ''
ORDER BY fqtn;
