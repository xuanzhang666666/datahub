import React, { useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useOnSelectionChange, useStore } from 'reactflow';
import styled from 'styled-components/macro';

import translateFieldPath from '@app/entityV2/dataset/profile/schema/utils/translateFieldPath';
import {
    FineGrainedOperationRef,
    LineageDisplayContext,
    LineageEntity,
    LineageNodesContext,
    parseColumnRef,
} from '@app/lineageV3/common';
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

export default function LineageSidebar() {
    const { rootUrn, nodes } = useContext(LineageNodesContext);
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
    }, [rootUrn]);

    // This manages closing, rather than isClosed
    if (!sidebarEntity) {
        return null;
    }

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
            <SidebarWrapper $distanceFromTop={0}>
                <CompactContext.Provider key={sidebarEntity.urn} value>
                    {entityRegistry.renderProfile(sidebarEntity.type, sidebarEntity.urn)}
                </CompactContext.Provider>
            </SidebarWrapper>
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
