<script setup lang="ts">
import { watchDebounced } from '@vueuse/core'
import { LoadingIndicator, Checkbox } from 'frappe-ui'
import { ArrowDownAZ, ArrowUpAZ, ClipboardPaste, ListChecks, SearchIcon } from 'lucide-vue-next'
import { computed, ref } from 'vue'

const props = defineProps<{
	valuesProvider: (search: string) => Promise<string[]>
}>()
const selectedValues = defineModel<string[]>({
	type: Array,
	default: () => [],
})

const distinctColumnValues = ref<any[]>([])
const searchInput = ref('')
const fetchingValues = ref(false)
const sortOrder = ref<'asc' | 'desc'>('asc')
const mode = ref<'list' | 'paste'>('list')
const pastedText = ref('')
const pasteFeedback = ref('')

watchDebounced(
	() => searchInput.value,
	(searchTxt) => {
		fetchingValues.value = true
		props
			.valuesProvider(searchTxt)
			.then((values: string[]) => (distinctColumnValues.value = values))
			.finally(() => (fetchingValues.value = false))
	},
	{ debounce: 300, immediate: true },
)

const selection = computed(() => selectedValues.value || [])

function compareValues(a: any, b: any) {
	const order = sortOrder.value === 'asc' ? 1 : -1
	const stringA = String(a)
	const stringB = String(b)

	// try numeric comparison first
	const numA = Number(stringA)
	const numB = Number(stringB)

	if (!isNaN(numA) && !isNaN(numB) && stringA.trim() && stringB.trim()) {
		return (numA - numB) * order
	}

	return (
		stringA.toLowerCase().localeCompare(stringB.toLowerCase(), undefined, { numeric: true }) *
		order
	)
}

const sortedValues = computed(() => [...distinctColumnValues.value].sort(compareValues))

// keep the selected values pinned on top, so that values which are not part of
// the fetched (or truncated) list are still visible & removable
const visibleValues = computed(() => {
	const search = searchInput.value.trim().toLowerCase()
	const selected = selection.value
		.filter((v) => !search || String(v).toLowerCase().includes(search))
		.sort(compareValues)

	const selectedSet = new Set(selection.value)
	const unselected = sortedValues.value.filter((v) => !selectedSet.has(v)).slice(0, 50)

	return [...selected, ...unselected]
})

function toggleValue(value: string) {
	if (selection.value.includes(value)) {
		selectedValues.value = selection.value.filter((v) => v !== value)
	} else {
		selectedValues.value = [...selection.value, value]
	}
}

function clearSelection() {
	selectedValues.value = []
}

function toggleSort() {
	sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
}

function parsePastedValues(text: string) {
	// if the text has line breaks, split only on those, so that values
	// containing a comma (or a semicolon) are not broken apart
	const separator = /[\r\n]/.test(text) ? /[\r\n]+/ : /[,;\t]+/
	return text
		.split(separator)
		.map((v) =>
			v
				.trim()
				.replace(/^["']|["']$/g, '')
				.trim(),
		)
		.filter(Boolean)
}

const parsedPastedValues = computed(() => {
	const values = parsePastedValues(pastedText.value)
	return [...new Set(values)]
})

function addPastedValues() {
	const existing = new Set(selection.value)
	const fresh = parsedPastedValues.value.filter((v) => !existing.has(v))
	const duplicates = parsedPastedValues.value.length - fresh.length

	if (fresh.length) {
		selectedValues.value = [...selection.value, ...fresh]
	}

	pasteFeedback.value = [
		`${fresh.length} value${fresh.length === 1 ? '' : 's'} added`,
		duplicates ? `${duplicates} already selected` : '',
	]
		.filter(Boolean)
		.join(', ')

	pastedText.value = ''
}

function switchMode(newMode: 'list' | 'paste') {
	mode.value = newMode
	if (newMode === 'list') {
		pasteFeedback.value = ''
	}
}
</script>

<template>
	<div class="flex flex-col gap-2">
		<div v-if="mode === 'list'" class="flex items-center gap-2">
			<FormControl
				placeholder="Search"
				v-model="searchInput"
				autocomplete="off"
				class="flex-1"
			>
				<template #prefix>
					<SearchIcon class="h-4 w-4 text-gray-400" />
				</template>
				<template #suffix>
					<LoadingIndicator v-if="fetchingValues" class="h-4 w-4 text-gray-600" />
				</template>
			</FormControl>
			<button
				@click.stop="toggleSort"
				class="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-gray-300 bg-white hover:bg-gray-50"
				:title="sortOrder === 'asc' ? 'Sort descending' : 'Sort ascending'"
			>
				<component :is="sortOrder === 'asc' ? ArrowDownAZ : ArrowUpAZ" class="h-4 w-4" />
			</button>
			<button
				@click.stop="switchMode('paste')"
				class="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-gray-300 bg-white hover:bg-gray-50"
				title="Paste a list of values"
			>
				<ClipboardPaste class="h-4 w-4" />
			</button>
		</div>

		<div v-else class="flex items-center gap-2">
			<span class="flex-1 text-sm text-gray-600">
				Paste values, one per line (or comma separated)
			</span>
			<button
				@click.stop="switchMode('list')"
				class="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded border border-gray-300 bg-white hover:bg-gray-50"
				title="Back to the list"
			>
				<ListChecks class="h-4 w-4" />
			</button>
		</div>

		<div
			v-if="selection.length"
			class="flex items-center justify-between gap-2 text-sm text-gray-600"
		>
			<span>{{ selection.length }} selected</span>
			<button class="hover:text-gray-800 hover:underline" @click.stop="clearSelection">
				Clear all
			</button>
		</div>

		<template v-if="mode === 'list'">
			<div class="max-h-[10rem] overflow-y-scroll">
				<div
					v-for="(value, idx) in visibleValues"
					:key="value || idx"
					class="flex cursor-pointer items-center justify-between gap-2 rounded px-1 py-1.5 text-base hover:bg-gray-100"
					@click.prevent.stop="toggleValue(value)"
				>
					<Checkbox
						class="pointer-events-none cursor-pointer duration-300"
						:modelValue="selection.includes(value)"
					/>
					<span class="flex-1 truncate"> {{ value }} </span>
				</div>
			</div>
		</template>

		<template v-else>
			<FormControl
				type="textarea"
				:rows="5"
				placeholder="BARCODE-001&#10;BARCODE-002&#10;BARCODE-003"
				v-model="pastedText"
				autocomplete="off"
			/>
			<div class="flex items-center justify-between gap-2">
				<span class="truncate text-sm text-gray-600">{{ pasteFeedback }}</span>
				<Button
					variant="subtle"
					:disabled="!parsedPastedValues.length"
					@click.stop="addPastedValues"
				>
					Add {{ parsedPastedValues.length }} value{{
						parsedPastedValues.length === 1 ? '' : 's'
					}}
				</Button>
			</div>
		</template>
	</div>
</template>
