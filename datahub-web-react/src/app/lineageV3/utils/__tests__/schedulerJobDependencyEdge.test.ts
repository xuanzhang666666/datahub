import { describe, expect, it } from 'vitest';

import {
    getDataJobIdFromUrn,
    getSchedulerJobDependencyCondition,
} from '@app/lineageV3/utils/schedulerJobDependencyEdge';
import { FetchedEntityV2 } from '@app/lineageV3/types';

import { EntityType } from '@types';

describe('schedulerJobDependencyEdge', () => {
    it('extracts data job id from urn', () => {
        expect(
            getDataJobIdFromUrn(
                'urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),PDW_Example_Job)',
            ),
        ).toBe('PDW_Example_Job');
    });

    it('reads dependency condition from downstream custom properties', () => {
        const downstream: FetchedEntityV2 = {
            urn: 'urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),downstream_job)',
            type: EntityType.DataJob,
            name: 'downstream_job',
            genericEntityProperties: {
                customProperties: [
                    {
                        key: 'scheduler_job_dependencies',
                        value:
                            '[{"upstream":"PDW_Example_Job","condition":"d = @$","status":"SUCCESS"}]',
                    },
                ],
            },
        };

        expect(
            getSchedulerJobDependencyCondition(
                downstream,
                'urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),PDW_Example_Job)',
            ),
        ).toBe('d = @$');
    });
});
