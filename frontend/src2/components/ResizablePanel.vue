<script setup lang="ts">
import { useStorage, useWindowSize } from '@vueuse/core'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
	defineProps<{
		side: 'left' | 'right'
		storageKey: string
		defaultWidth?: number
		minWidth?: number
		maxWidth?: number
		autoCollapseBelow?: number
	}>(),
	{
		defaultWidth: 280,
		minWidth: 220,
		maxWidth: 480,
		autoCollapseBelow: undefined,
	},
)

const width = useStorage(`insights:panel-width:${props.storageKey}`, props.defaultWidth)
const collapsed = useStorage(`insights:panel-collapsed:${props.storageKey}`, false)

const { width: windowWidth } = useWindowSize()

width.value = Math.min(Math.max(width.value, props.minWidth), props.maxWidth)

if (props.autoCollapseBelow) {
	watch(windowWidth, (w) => {
		if (props.autoCollapseBelow && w < props.autoCollapseBelow) {
			collapsed.value = true
		}
	})
}

const isDragging = ref(false)
let dragMoved = false
let dragStartX = 0
let dragStartWidth = 0

function clampWidth(value: number) {
	const viewportMax = Math.round(windowWidth.value * 0.5)
	return Math.min(Math.max(value, props.minWidth), Math.min(props.maxWidth, viewportMax))
}

function onHandleMousedown(e: MouseEvent) {
	e.preventDefault()
	isDragging.value = true
	dragMoved = false
	dragStartX = e.clientX
	dragStartWidth = width.value
	document.body.classList.add('select-none')
	window.addEventListener('mousemove', onMousemove)
	window.addEventListener('mouseup', onMouseup)
}

function onMousemove(e: MouseEvent) {
	const delta = e.clientX - dragStartX
	if (Math.abs(delta) > 3) dragMoved = true
	if (!dragMoved) return
	if (collapsed.value) collapsed.value = false
	const signedDelta = props.side === 'left' ? delta : -delta
	width.value = clampWidth(dragStartWidth + signedDelta)
}

function stopDragging() {
	isDragging.value = false
	document.body.classList.remove('select-none')
	window.removeEventListener('mousemove', onMousemove)
	window.removeEventListener('mouseup', onMouseup)
}

function onMouseup() {
	const wasMoved = dragMoved
	stopDragging()
	if (!wasMoved) {
		collapsed.value = !collapsed.value
	}
}

onBeforeUnmount(stopDragging)

const pointsLeft = computed(() => (props.side === 'left' ? !collapsed.value : collapsed.value))
</script>

<template>
	<div class="relative flex h-full flex-shrink-0">
		<div
			class="h-full overflow-hidden"
			:class="{ 'transition-[width] duration-150 ease-in-out': !isDragging }"
			:style="{ width: (collapsed ? 0 : width) + 'px', order: side === 'left' ? 1 : 2 }"
		>
			<div class="h-full w-full">
				<slot />
			</div>
		</div>
		<div
			class="group relative z-10 flex h-full w-1.5 flex-shrink-0 cursor-col-resize items-center justify-center hover:bg-gray-200"
			:style="{ order: side === 'left' ? 2 : 1 }"
			@mousedown="onHandleMousedown"
		>
			<div
				class="flex h-10 w-4 items-center justify-center rounded bg-gray-100 text-gray-500 opacity-0 group-hover:opacity-100"
			>
				<ChevronLeft v-if="pointsLeft" class="h-3 w-3" stroke-width="1.5" />
				<ChevronRight v-else class="h-3 w-3" stroke-width="1.5" />
			</div>
		</div>
	</div>
</template>
