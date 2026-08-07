<script setup lang="ts">
import { ListFilter } from 'lucide-vue-next'
import { computed, inject, onMounted, ref } from 'vue'
import { confirmDialog } from '../helpers/confirm_dialog'
import { createToast } from '../helpers/toasts'
import { __ } from '../translation'
import { Dashboard, FilterPreset } from './dashboard'
import DashboardSaveFilterPresetDialog from './DashboardSaveFilterPresetDialog.vue'

const dashboard = inject<Dashboard>('dashboard')!

const hasFilters = computed(() => dashboard.doc.items.some((item) => item.type === 'filter'))

const hasActiveFilterValues = computed(() =>
	Object.values(dashboard.filterStates).some(
		(f) => f?.value !== undefined && f?.value !== null && f?.value !== '',
	),
)

onMounted(() => {
	if (hasFilters.value) dashboard.loadFilterPresets()
})

const showSaveDialog = ref(false)
const editingPreset = ref<FilterPreset>()
function openSaveDialog(preset?: FilterPreset) {
	editingPreset.value = preset
	showSaveDialog.value = true
}

function canUpdate(preset: FilterPreset) {
	return (
		preset.is_mine &&
		JSON.stringify(dashboard.filterStates) !== JSON.stringify(preset.filter_values)
	)
}
function canDelete(preset: FilterPreset) {
	return preset.is_mine || (preset.is_shared && !dashboard.doc.read_only)
}

function applyPreset(preset: FilterPreset) {
	dashboard.applyFilterPreset(preset)
}

async function toggleDefault(preset: FilterPreset) {
	await dashboard.setDefaultFilterPreset(preset.is_default ? null : preset)
}

async function updateWithCurrentFilters(preset: FilterPreset) {
	await dashboard.updateFilterPreset(preset, { filter_values: dashboard.filterStates })
	createToast({ variant: 'success', title: __('Preset updated with current filters') })
}

function deletePreset(preset: FilterPreset) {
	confirmDialog({
		title: __('Delete Saved Filter'),
		message: __('Are you sure you want to delete "{0}"?', preset.preset_name),
		onSuccess: () => dashboard.deleteFilterPreset(preset),
	})
}

function presetOptions(preset: FilterPreset) {
	return [
		{
			label: preset.is_default ? __('Remove as default') : __('Set as default'),
			icon: 'star',
			onClick: () => toggleDefault(preset),
		},
		canUpdate(preset)
			? {
					label: __('Update with current filters'),
					icon: 'refresh-ccw',
					onClick: () => updateWithCurrentFilters(preset),
			  }
			: null,
		preset.is_mine
			? {
					label: __('Rename'),
					icon: 'edit-2',
					onClick: () => openSaveDialog(preset),
			  }
			: null,
		canDelete(preset)
			? {
					label: __('Delete'),
					icon: 'trash-2',
					onClick: () => deletePreset(preset),
			  }
			: null,
	]
}
</script>

<template>
	<Popover v-if="hasFilters" placement="bottom-end">
		<template #target="{ togglePopover }">
			<Button variant="outline" @click="togglePopover">
				<template #prefix>
					<ListFilter class="h-4 w-4 text-gray-700" stroke-width="1.5" />
				</template>
				{{ __('Saved Filters') }}
			</Button>
		</template>
		<template #body-main="{ togglePopover }">
			<div class="flex w-72 flex-col gap-1 p-2">
				<div
					v-if="!dashboard.filterPresets.length"
					class="px-2 py-3 text-center text-sm text-gray-600"
				>
					{{ __('No saved filters yet') }}
				</div>
				<div
					v-for="preset in dashboard.filterPresets"
					:key="preset.name"
					class="flex items-center gap-1 rounded px-2 py-1.5 hover:bg-gray-100"
				>
					<button
						class="flex flex-1 items-center gap-2 truncate text-left text-sm"
						@click="
							() => {
								applyPreset(preset)
								togglePopover()
							}
						"
					>
						<span class="truncate">{{ preset.preset_name }}</span>
						<Badge v-if="preset.is_shared" theme="blue" size="sm">{{
							__('Shared')
						}}</Badge>
						<Badge v-if="preset.is_default" theme="green" size="sm">{{
							__('Default')
						}}</Badge>
					</button>
					<Dropdown
						:options="presetOptions(preset)"
						:button="{ icon: 'more-horizontal', variant: 'ghost' }"
					/>
				</div>
				<hr class="my-1 border-t border-gray-200" />
				<Button
					variant="ghost"
					class="justify-start"
					icon-left="plus"
					:disabled="!hasActiveFilterValues"
					@click="
						() => {
							openSaveDialog()
							togglePopover()
						}
					"
				>
					{{ __('Save current filters as…') }}
				</Button>
			</div>
		</template>
	</Popover>

	<DashboardSaveFilterPresetDialog
		v-if="showSaveDialog"
		v-model="showSaveDialog"
		:existing-preset="editingPreset"
	/>
</template>
