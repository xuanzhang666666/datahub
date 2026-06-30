# DataHub Task Lineage Edge Condition UI SOP

Use this reference when task dependency edges in the DataHub lineage UI no longer show the upstream task satisfaction condition, for example `d = @$`, on the graph edge.

## Symptom

In the Task lineage graph, upstream and downstream tasks are connected, but the edge label showing the scheduler dependency condition is missing.

## Expected Data Source

The backend data normally already exists. Verify before changing frontend code:

- DataJob custom property key: `scheduler_job_dependencies`
- Graph edge property key: `blf_schedule_dependency_condition`
- Example condition: `d = @$`

If these values exist in MySQL / Elasticsearch but the UI does not show labels, the problem is usually the frontend bundle or the lineage edge rendering code.

## Root Cause Seen On 2026-06-30

The deployed frontend image had two assets jars under `/datahub-frontend/lib`:

- `datahub-web-react-datahub-web-react-1.5.0-4-assets.jar`
- `datahub-web-react-datahub-web-react-1.5.0.5-SNAPSHOT-assets.jar`

The service actually loaded the latter jar, while an earlier repair image only replaced the former. The UI therefore kept serving the old bundle `assets/index-BRj5QbIF.js`, which did not contain:

- `scheduler_job_dependencies`
- `dependencyCondition`
- `LineageEdgeConditionLabel`

The fixed image replaced both jar filenames and served `assets/index-Dv1xFYqR.js`.

## Current Good Frontend Image

As of 2026-06-30, both neo4j2 and sc7 should run:

```text
datahub-frontend-react:lineage-sidebar-v1504-j17-v10-assets-condition-202606300907
```

This image is based on:

```text
datahub-frontend-react:lineage-sidebar-v1504-j17-v3-202605191037
```

It overlays the patched DataHub v1.5.0.4-compatible `datahub-web-react` assets jar into both runtime jar paths.

## Frontend Code Requirements

The UI fix must do all of the following:

1. Parse downstream DataJob `genericEntityProperties.customProperties`.
2. Read the `scheduler_job_dependencies` JSON property.
3. Extract upstream job id from URNs like:
   `urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),dw_order_v1_di)`
4. Attach the matched condition to lineage edge data as `dependencyCondition`.
5. Render the condition in V2 and V3 lineage edge components.
6. Keep a regression test for the parser.

Known local patched worktree from the 2026-06-30 repair:

```text
/tmp/datahub-v1504-lineage-condition
```

Important files in that worktree:

```text
datahub-web-react/src/app/lineageV3/utils/schedulerJobDependencyEdge.ts
datahub-web-react/src/app/lineageV3/LineageEdge/LineageEdgeConditionLabel.tsx
datahub-web-react/src/app/lineageV3/utils/__tests__/schedulerJobDependencyEdge.test.ts
datahub-web-react/src/app/lineageV2/NodeBuilder.ts
datahub-web-react/src/app/lineageV3/useComputeGraph/NodeBuilder.ts
datahub-web-react/src/app/lineageV2/LineageEdge/LineageTableEdge.tsx
datahub-web-react/src/app/lineageV3/LineageEdge/LineageTableEdge.tsx
datahub-web-react/src/app/lineageV3/LineageEdge/DataJobInputOutputEdge.tsx
```

## Build Rule

For v1.5.0.4 production, build from a v1.5.0.4-compatible source tree with Java 17:

```bash
JAVA_HOME=/Users/zhangxuan/.sdkman/candidates/java/17.0.13-tem ./gradlew :datahub-web-react:jar
```

Do not replace the full frontend server jar with a newer branch build unless Java/runtime compatibility has been verified. A newer branch build previously failed on the production Java 17 runtime with Java class version 65.

## Image Overlay Pattern

Build an assets-only image from the known production frontend image and replace both assets jar names:

```Dockerfile
FROM datahub-frontend-react:lineage-sidebar-v1504-j17-v3-202605191037
USER root
COPY --chown=datahub:datahub assets-only/datahub-web-react-1.5.0.5-SNAPSHOT-assets-lineage-condition.jar /datahub-frontend/lib/datahub-web-react-datahub-web-react-1.5.0-4-assets.jar
COPY --chown=datahub:datahub assets-only/datahub-web-react-1.5.0.5-SNAPSHOT-assets-lineage-condition.jar /datahub-frontend/lib/datahub-web-react-datahub-web-react-1.5.0.5-SNAPSHOT-assets.jar
USER datahub
```

Suggested image tag format:

```text
datahub-frontend-react:lineage-sidebar-v1504-j17-v<next>-assets-condition-<yyyymmddHHMM>
```

## Required Verification

Verify the image before deploying:

```bash
docker run --rm --entrypoint sh <image> -lc '
for jar in \
  /datahub-frontend/lib/datahub-web-react-datahub-web-react-1.5.0-4-assets.jar \
  /datahub-frontend/lib/datahub-web-react-datahub-web-react-1.5.0.5-SNAPSHOT-assets.jar
do
  echo "JAR:$jar"
  unzip -p "$jar" | grep -aoE "scheduler_job_dependencies|dependencyCondition" | sort | uniq -c
done
'
```

Expected result for each jar:

```text
8 dependencyCondition
1 scheduler_job_dependencies
```

After deploying, verify the actual served bundle:

```bash
curl -s -o /tmp/datahub_frontend_home.html -w "%{http_code}" http://127.0.0.1:9002/
python3 <<'PY'
import re
import urllib.request
html = open('/tmp/datahub_frontend_home.html').read()
match = re.search(r'assets/index-[^"\']+\.js', html)
print('js=' + (match.group(0) if match else 'MISSING'))
if match:
    bundle = urllib.request.urlopen('http://127.0.0.1:9002/' + match.group(0)).read().decode('utf-8', 'ignore')
    print('scheduler_job_dependencies=', bundle.count('scheduler_job_dependencies'))
    print('dependencyCondition=', bundle.count('dependencyCondition'))
PY
```

Expected deployed result:

```text
HTTP 200
scheduler_job_dependencies= 1
dependencyCondition= 8
```

Also wait for:

```text
datahub-datahub-frontend-react-1 ... (healthy)
```

## Deployment Checklist

1. Back up `/data/datahub/docker-compose.yml`.
2. Update only the frontend image tag unless the user asked for broader changes.
3. Run `docker compose up -d datahub-frontend-react`.
4. Verify HTTP 200, actual bundle counts, and container health.
5. Repeat the same image and verification on sc7 if sc7 must stay consistent with neo4j2.
6. If the page returns 404 or the bundle lacks the strings above, roll back to the previous compose backup or the last known good image.

## Final Answer Evidence

When reporting completion, include:

- host(s) updated
- frontend image tag
- actual served bundle name
- counts for `scheduler_job_dependencies` and `dependencyCondition`
- container health status
