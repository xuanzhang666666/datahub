import { colors } from '@components';
import { X } from '@phosphor-icons/react';
import React, { useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { useOnSelectionChange, useStore } from 'reactflow';
import styled from 'styled-components/macro';

import translateFieldPath from '@app/entityV2/dataset/profile/schema/utils/translateFieldPath';
import SidebarQueryOperationsSection from '@app/entityV2/shared/containers/profile/sidebar/Query/SidebarQueryOperationsSection';
import {
    FineGrainedOperationRef,
    LineageDisplayContext,
    LineageEntity,
    LineageNodesContext,
    parseColumnRef,
} from '@app/lineageV2/common';
import CompactContext from '@app/shared/CompactContext';
import EntitySidebarContext, { FineGrainedOperation } from '@app/sharedV2/EntitySidebarContext';
import useSidebarWidth from '@app/sharedV2/sidebar/useSidebarWidth';
import { useEntityRegistry } from '@app/useEntityRegistry';

const SidebarWrapper = styled.div<{ $distanceFromTop: number }>`
    position: absolute;
    right: 0;
    top: 0;
    display: flex;
    flex-direction: column;
    z-index: 1;
    height: 100vh;

    && {
        &::-webkit-scrollbar {
            display: none;
        }
    }
`;

const ColumnLogicWrapper = styled.div`
    background: #fff;
    border-left: 1px solid ${colors.gray[100]};
    box-shadow: -2px 0 8px rgba(0, 0, 0, 0.08);
    display: flex;
    flex-direction: column;
    height: 100%;
    min-width: 320px;
    max-width: 400px;
    overflow-y: auto;
`;

const ColumnLogicHeader = styled.div`
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid ${colors.gray[100]};
    font-weight: 600;
    font-size: 14px;
    color: ${colors.gray[700]};
    flex-shrink: 0;
`;

const CloseButton = styled.button`
    background: none;
    border: none;
    cursor: pointer;
    padding: 2px;
    display: flex;
    align-items: center;
    color: ${colors.gray[500]};
    &:hover {
        color: ${colors.gray[800]};
    }
`;

const ColumnLogicBody = styled.div`
    padding: 8px 4px;
    overflow-y: auto;
    flex: 1;
`;

interface Props {
    urn: string;
}

export default function LineageSidebar({ urn }: Props) {
    const { nodes } = useContext(LineageNodesContext);
    const { selectedColumn } = useContext(LineageDisplayContext);
    const entityRegistry = useEntityRegistry();
    const [selectedEntity, setSelectedEntity] = useSelectedNode();
    const resetSelectedElements = useStore((actions) => actions.resetSelectedElements);
    const sidebarEntity = useMemo(
        () => selectedEntity || getSelectedColumnEntity(selectedColumn, nodes),
        [selectedColumn, selectedEntity, nodes],
    );
    const queryDetails = useQueryDetails(sidebarEntity);
    const width = useSidebarWidth();

    const setSidebarClosed = useCallback(
        (closed) => {
            if (closed) {
                resetSelectedElements();
                setSelectedEntity(null);
            }
        },
        [resetSelectedElements, setSelectedEntity],
    );

    useEffect(() => {
        setSidebarClosed(true);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [urn]);

    if (!sidebarEntity) {
        return null;
    }

    // Column click (no table node selected): show lightweight LOGIC-only panel
    if (selectedColumn && !selectedEntity) {
        if (!queryDetails?.length) {
            return null;
        }
        return (
            <EntitySidebarContext.Provider
                value={{
                    width,
                    isClosed: false,
                    setSidebarClosed,
                    forLineage: true,
                    separateSiblings: false,
                    fineGrainedOperations: queryDetails,
                }}
            >
                {createPortal(
                    <SidebarWrapper $distanceFromTop={0}>
                        <ColumnLogicWrapper>
                            <ColumnLogicHeader>
                                字段加工逻辑
                                <CloseButton onClick={() => setSidebarClosed(true)}>
                                    <X size={16} />
                                </CloseButton>
                            </ColumnLogicHeader>
                            <ColumnLogicBody>
                                <SidebarQueryOperationsSection />
                            </ColumnLogicBody>
                        </ColumnLogicWrapper>
                    </SidebarWrapper>,
                    document.body,
                )}
            </EntitySidebarContext.Provider>
        );
    }

    // Table node selected: show full entity profile
    return (
        <EntitySidebarContext.Provider
            value={{
                width,
                isClosed: false,
                setSidebarClosed,
                forLineage: true,
                separateSiblings: !sidebarEntity.entity?.lineageSiblingIcon,
                fineGrainedOperations: queryDetails,
            }}
        >
            {createPortal(
                <SidebarWrapper $distanceFromTop={0}>
                    <CompactContext.Provider key={sidebarEntity.urn} value>
                        {entityRegistry.renderProfile(sidebarEntity.type, sidebarEntity.urn)}
                    </CompactContext.Provider>
                </SidebarWrapper>,
                document.body,
            )}
        </EntitySidebarContext.Provider>
    );
}

function useSelectedNode(): [LineageEntity | null, (v: LineageEntity | null) => void] {
    // Entity Profile sidebar, not lineage sidebar
    const { setSidebarClosed } = useContext(EntitySidebarContext);
    const [selectedNode, setSelectedNode] = useState<LineageEntity | null>(null);

    useOnSelectionChange({
        onChange: ({ nodes }) => {
            if (nodes.length) setSidebarClosed(true);
            setSelectedNode(nodes.length ? nodes[nodes.length - 1].data : null);
        },
    });

    return [selectedNode, setSelectedNode];
}

function getSelectedColumnEntity(
    selectedColumn: string | null,
    nodes: Map<string, LineageEntity>,
): LineageEntity | null {
    if (!selectedColumn) {
        return null;
    }
    const [columnUrn] = parseColumnRef(selectedColumn);
    return nodes.get(columnUrn) || null;
}

function useQueryDetails(selectedNode: LineageEntity | null): FineGrainedOperation[] | undefined {
    const { nodes } = useContext(LineageNodesContext);
    const { cllHighlightedNodes, fineGrainedOperations, selectedColumn } = useContext(LineageDisplayContext);

    const operationRefs = collectOperationRefsForSidebar(
        selectedColumn,
        selectedNode,
        cllHighlightedNodes,
        fineGrainedOperations,
    );
    if (!operationRefs.length) {
        return undefined;
    }

    return operationRefs.map((ref) => {
        const data = fineGrainedOperations.get(ref);
        return {
            inputColumns: getColumnNames(nodes, data?.inputColumns),
            outputColumns: getColumnNames(nodes, data?.outputColumns),
            transformOperation: data?.transformOperation,
        };
    });
}

function collectOperationRefsForSidebar(
    selectedColumn: string | null,
    selectedNode: LineageEntity | null,
    cllHighlightedNodes: Map<string, Set<FineGrainedOperationRef> | null>,
    fineGrainedOperations: Map<FineGrainedOperationRef, FineGrainedOperation>,
): FineGrainedOperationRef[] {
    if (selectedColumn) {
        const [selectedColumnUrn, selectedColumnPath] = parseColumnRef(selectedColumn);
        const refs = new Set<FineGrainedOperationRef>();
        cllHighlightedNodes.forEach((nodeRefs) => {
            nodeRefs?.forEach((ref) => {
                const operation = fineGrainedOperations.get(ref);
                if (
                    operation?.outputColumns?.some(
                        ([urn, path]) => urn === selectedColumnUrn && path === selectedColumnPath,
                    )
                ) {
                    refs.add(ref);
                }
            });
        });
        return Array.from(refs);
    }
    if (selectedNode) {
        return Array.from(cllHighlightedNodes.get(selectedNode.urn) || []);
    }
    return [];
}

// TODO: Clean this up
function getColumnNames(
    nodes: Map<string, LineageEntity>,
    columns?: Array<[string, string]>,
): Array<[string, string]> | undefined {
    return columns?.map(([urn, column]) => [nodes.get(urn)?.entity?.name || urn, translateFieldPath(column)]);
}
