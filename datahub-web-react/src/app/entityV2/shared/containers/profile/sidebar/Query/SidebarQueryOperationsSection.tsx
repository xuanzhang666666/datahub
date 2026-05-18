import React, { useContext } from 'react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import styled from 'styled-components';

import { SidebarSection } from '@app/entityV2/shared/containers/profile/sidebar/SidebarSection';
import EntitySidebarContext, { FineGrainedOperation } from '@app/sharedV2/EntitySidebarContext';

const ONE_LINE_CHINESE_EXPLANATION_COMMENT = /^(\s*)\/\*\s*(中文解释：.*?)\s*\*\/\s*$/;

export default function SidebarQueryOperationsSection() {
    const { fineGrainedOperations } = useContext(EntitySidebarContext);

    if (!fineGrainedOperations?.length) {
        return null;
    }

    return (
        <>
            {fineGrainedOperations.map((operation: FineGrainedOperation, index) => (
                <SidebarSection
                    title={`字段加工逻辑 ${index + 1}`}
                    /* eslint-disable-next-line react/no-array-index-key */
                    key={index}
                    content={<SidebarQueryOperation operation={operation} />}
                />
            ))}{' '}
        </>
    );
}

const OperationContainer = styled.div`
    display: flex;
    flex-direction: column;
    gap: 10px;
`;

const Section = styled.div`
    display: flex;
    flex-direction: column;
    align-items: start;
    justify-content: start;
    margin-bottom: 6px;
    text-wrap: wrap;
`;

const SectionHeader = styled.div`
    display: flex;
    height: 20px;
    color: ${(props) => props.theme.colors.textSecondary};
    font-size: 12px;
    font-weight: 700;
    line-height: 20px;
    letter-spacing: 0.04em;
`;

const HeaderColumn = styled.th`
    padding: 0 0.5em 0 0;
    text-align: left;
    vertical-align: top;
    font-weight: normal;
    color: ${(props) => props.theme.colors.textSecondary};
`;

const TextColumn = styled.td`
    text-wrap: wrap;
    font-family: 'Roboto Mono', monospace;
`;

const PreviewSyntax = styled(SyntaxHighlighter)`
    max-width: 100%;
    max-height: 150px;
    overflow: hidden;
    white-space: pre-wrap;
    overflow-wrap: anywhere;

    span {
        font-family: 'Roboto Mono', monospace;
    }
`;

export function formatTransformOperationForDisplay(transformOperation: string) {
    return transformOperation
        .split('\n')
        .map((line) => {
            const match = line.match(ONE_LINE_CHINESE_EXPLANATION_COMMENT);
            if (!match) {
                return line;
            }

            const [, indent, explanation] = match;
            return `${indent}/*\n${indent}${explanation}\n${indent}*/`;
        })
        .join('\n');
}

function SidebarQueryOperation({ operation }: { operation: FineGrainedOperation }) {
    return (
        <OperationContainer>
            {operation.transformOperation && (
                <Section key="logic">
                    <SectionHeader>LOGIC</SectionHeader>
                    <PreviewSyntax
                        language="sql"
                        showLineNumbers
                        wrapLines
                        wrapLongLines
                        lineNumberStyle={{ display: 'none' }}
                    >
                        {formatTransformOperationForDisplay(operation.transformOperation)}
                    </PreviewSyntax>
                </Section>
            )}
            {operation.inputColumns?.length && (
                <OperationInputsOrOutputs title="inputs" columns={operation.inputColumns} />
            )}
            {operation.outputColumns?.length && (
                <OperationInputsOrOutputs title="outputs" columns={operation.outputColumns} />
            )}
        </OperationContainer>
    );
}

function OperationInputsOrOutputs({ title, columns }: { title: string; columns: Array<[string, string]> }) {
    const tables = Array.from(new Set(columns.map(([name]) => name)));
    return (
        <Section key={title}>
            <SectionHeader>{title.toLocaleUpperCase()}</SectionHeader>
            <div>
                <table cellSpacing={0} cellPadding={0}>
                    <tbody>
                        <tr>
                            <HeaderColumn>Tables:</HeaderColumn>
                            <TextColumn>{tables.join(', ')}</TextColumn>
                        </tr>
                        <tr>
                            <HeaderColumn>Columns:</HeaderColumn>
                            <TextColumn>{columns.map(([, col]) => col).join(', ')}</TextColumn>
                        </tr>
                    </tbody>
                </table>
            </div>
        </Section>
    );
}
