<script setup lang="ts">
import { inject, onBeforeUnmount } from 'vue'
import { Query } from '../query'
import QueryBuilderSourceSelector from './QueryBuilderSourceSelector.vue'
import QueryBuilderTable from './QueryBuilderTable.vue'
import QueryExecutionStatus from './QueryExecutionStatus.vue'
import QueryToolbar from './QueryToolbar.vue'
import QueryInfo from './QueryInfo.vue'
import QueryOperations from './QueryOperations.vue'
import { useMagicKeys } from '@vueuse/core'
import { whenever } from '@vueuse/core'
import ResizablePanel from '../../components/ResizablePanel.vue'

const query = inject<Query>('query')!
query.autoExecute = true

const keys = useMagicKeys()
const cmdZ = keys['Meta+Z']
const cmdShiftZ = keys['Meta+Shift+Z']
const stopUndoWatcher = whenever(cmdZ, () => query.canUndo() && query.history.undo())
const stopRedoWatcher = whenever(cmdShiftZ, () => query.canRedo() && query.history.redo())

onBeforeUnmount(() => {
	query.activeOperationIdx = query.doc.operations.length - 1
	stopUndoWatcher()
	stopRedoWatcher()
})
</script>

<template>
	<div class="flex flex-1 overflow-hidden">
		<div class="relative flex h-full flex-1 flex-col gap-3 overflow-hidden p-4">
			<QueryBuilderSourceSelector v-if="!query.doc.operations.length" />
			<template v-else>
				<QueryToolbar>
					<QueryExecutionStatus />
				</QueryToolbar>
				<QueryBuilderTable></QueryBuilderTable>
			</template>
		</div>
		<ResizablePanel
			side="right"
			storage-key="query-right-panel"
			:default-width="304"
			:min-width="260"
			:max-width="480"
			:auto-collapse-below="768"
		>
			<div class="flex h-full w-full flex-col overflow-y-auto bg-white">
				<QueryInfo />
				<QueryOperations />
			</div>
		</ResizablePanel>
	</div>
</template>
