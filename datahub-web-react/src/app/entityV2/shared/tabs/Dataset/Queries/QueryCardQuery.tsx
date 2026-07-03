import React from 'react';
import styled from 'styled-components';

import { StyledSyntaxHighlighter } from '@app/entityV2/shared/StyledSyntaxHighlighter';

const Statement = styled.div<{ fullHeight?: boolean; isCompact?: boolean }>`
    background-color: ${(props) => props.theme.colors.bgSurface};
    height: ${(props) => (props.fullHeight && '378px') || '240px'};
    margin: 0px 0px 4px 0px;
    border-radius: 8px;
    :hover {
        cursor: pointer;
    }
    overflow: auto !important;

    ${(props) =>
        props.isCompact &&
        `
        height: auto;
        min-height: 120px;
        max-height: 360px;
        overflow: auto !important;
        margin: 0;
    `}
`;

const NestedSyntax = styled(StyledSyntaxHighlighter)<{ isCompact?: boolean }>`
    background-color: transparent !important;
    border: none !important;
    margin: 0px !important;
    height: 100% !important;
    overflow: auto !important;
    ::-webkit-scrollbar {
        display: none;
    } !important;

    ${(props) =>
        props.isCompact &&
        `
        white-space: pre-wrap !important;
        word-break: break-word;
        overflow: auto !important;
        padding: 12px !important;
    `}
`;

type Props = {
    query: string;
    showDetails: boolean;
    onClickExpand?: (newQuery) => void;
    index?: number;
    isCompact?: boolean;
};

function formatSqlForDisplay(query: string): string {
    if (!query) {
        return '';
    }

    if (query.includes('\n')) {
        return query.trim();
    }

    return query
        .trim()
        .replace(/\s+/g, ' ')
        .replace(/\b(select)\b/gi, '\n$1\n  ')
        .replace(/\b(from|where|having|limit)\b/gi, '\n$1')
        .replace(/\b(group\s+by|order\s+by)\b/gi, '\n$1')
        .replace(/\b(left\s+join|right\s+join|full\s+join|inner\s+join|cross\s+join|join)\b/gi, '\n  $1')
        .replace(/\b(on)\b/gi, '\n    $1')
        .replace(/\b(and|or)\b/gi, '\n    $1')
        .replace(/\b(union\s+all|union)\b/gi, '\n$1')
        .replace(/,\s*(?=[A-Za-z_][\w.]*|\?|case\b|sum\(|count\(|max\(|min\()/gi, ',\n  ')
        .replace(/\(\s*select\b/gi, '(\nselect')
        .replace(/\)\s*(select|from|where|group\s+by|order\s+by|having|limit)\b/gi, ')\n$1')
        .replace(/\n\s*\n/g, '\n')
        .trim()
        .replace(/^select\n\s+/i, 'select\n  ');
}

export default function QueryCardQuery({ query, showDetails, onClickExpand, index, isCompact }: Props) {
    const displayQuery = isCompact ? formatSqlForDisplay(query) : query;

    return (
        <Statement
            fullHeight={!showDetails}
            onClick={onClickExpand}
            data-testid={`query-content-${index}`}
            isCompact={isCompact}
        >
            <NestedSyntax showLineNumbers language="sql" isCompact={isCompact} wrapLongLines={isCompact}>
                {displayQuery}
            </NestedSyntax>
        </Statement>
    );
}
