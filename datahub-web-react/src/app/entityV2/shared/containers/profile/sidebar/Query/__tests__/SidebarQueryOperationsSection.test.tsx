import { formatTransformOperationForDisplay } from '@app/entityV2/shared/containers/profile/sidebar/Query/SidebarQueryOperationsSection';

describe('formatTransformOperationForDisplay', () => {
    it('breaks a one-line Chinese explanation block comment into display lines', () => {
        expect(formatTransformOperationForDisplay('/* 中文解释：表示门店类型。 */\nstore_type')).toBe(
            '/*\n中文解释：表示门店类型。\n*/\nstore_type',
        );
    });

    it('leaves existing multi-line block comments unchanged', () => {
        const transformOperation = '/*\n中文解释：表示门店类型。\n*/\nstore_type';

        expect(formatTransformOperationForDisplay(transformOperation)).toBe(transformOperation);
    });
});
