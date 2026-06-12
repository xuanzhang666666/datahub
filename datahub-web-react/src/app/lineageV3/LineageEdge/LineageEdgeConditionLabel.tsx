import React from 'react';
import styled from 'styled-components';

const Label = styled.div`
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    line-height: 14px;
    white-space: nowrap;
    color: ${(props) => props.theme.colors.textSecondary};
    background: ${(props) => props.theme.colors.bgSurface};
    border: 1px solid ${(props) => props.theme.colors.border};
`;

interface Props {
    label?: string;
}

export default function LineageEdgeConditionLabel({ label }: Props) {
    if (!label) {
        return null;
    }
    return <Label title={label}>{label}</Label>;
}
