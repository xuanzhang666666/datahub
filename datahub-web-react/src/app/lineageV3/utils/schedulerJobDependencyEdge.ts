import { FetchedEntityV2 } from '@app/lineageV3/types';

import { EntityType } from '@types';

const SCHEDULER_JOB_DEPENDENCIES_KEY = 'scheduler_job_dependencies';

interface SchedulerJobDependencyEntry {
    upstream?: string;
    condition?: string;
}

export function getDataJobIdFromUrn(urn: string): string | undefined {
    const prefix = 'urn:li:dataJob:(urn:li:dataFlow:(';
    if (!urn.startsWith(prefix)) {
        return undefined;
    }
    const closingIndex = urn.lastIndexOf(')');
    const commaIndex = urn.lastIndexOf(',', closingIndex - 1);
    if (commaIndex < 0 || closingIndex < 0) {
        return undefined;
    }
    return urn.slice(commaIndex + 1, closingIndex);
}

export function getSchedulerJobDependencyCondition(
    downstreamEntity: FetchedEntityV2 | undefined,
    upstreamUrn: string,
): string | undefined {
    if (downstreamEntity?.type !== EntityType.DataJob) {
        return undefined;
    }
    const customProperties = downstreamEntity.genericEntityProperties?.customProperties;
    const depsRaw = customProperties?.find((entry) => entry.key === SCHEDULER_JOB_DEPENDENCIES_KEY)?.value;
    if (!depsRaw) {
        return undefined;
    }
    const upstreamJobId = getDataJobIdFromUrn(upstreamUrn);
    if (!upstreamJobId) {
        return undefined;
    }
    try {
        const deps = JSON.parse(depsRaw) as SchedulerJobDependencyEntry[];
        return deps.find((dep) => dep.upstream === upstreamJobId)?.condition?.trim() || undefined;
    } catch {
        return undefined;
    }
}
