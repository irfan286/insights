<script setup lang="ts">
import { inject, ref } from 'vue'
import { __ } from '../translation'
import { Dashboard, FilterPreset } from './dashboard'

const props = defineProps<{ existingPreset?: FilterPreset }>()
const show = defineModel<boolean>()

const dashboard = inject<Dashboard>('dashboard')!

const presetName = ref(props.existingPreset?.preset_name || '')
const isShared = ref(props.existingPreset?.is_shared || false)
const isDefault = ref(props.existingPreset?.is_default || false)

const saving = ref(false)

async function save() {
	saving.value = true
	try {
		if (props.existingPreset) {
			await dashboard.updateFilterPreset(props.existingPreset, {
				preset_name: presetName.value,
				is_shared: isShared.value,
			})
			if (isDefault.value !== props.existingPreset.is_default) {
				await dashboard.setDefaultFilterPreset(
					isDefault.value ? props.existingPreset : null,
				)
			}
		} else {
			await dashboard.saveFilterPreset(presetName.value, isShared.value, isDefault.value)
		}
		show.value = false
	} finally {
		saving.value = false
	}
}
</script>

<template>
	<Dialog
		v-model="show"
		:options="{
			title: props.existingPreset ? __('Rename Saved Filter') : __('Save Current Filters'),
			actions: [
				{
					label: __('Save'),
					variant: 'solid',
					disabled: !presetName.trim(),
					loading: saving,
					onClick: save,
				},
			],
		}"
	>
		<template #body-content>
			<div class="flex flex-col gap-4">
				<FormControl
					:label="__('Preset Name')"
					v-model="presetName"
					:placeholder="__('e.g. This Quarter - APAC')"
					autocomplete="off"
				/>
				<div class="flex items-center justify-between">
					<span class="text-sm text-gray-700">{{
						__('Everyone can use this preset')
					}}</span>
					<Toggle v-model="isShared" />
				</div>
				<div class="flex items-center justify-between">
					<span class="text-sm text-gray-700">{{ __('Set as my default') }}</span>
					<Toggle v-model="isDefault" />
				</div>
			</div>
		</template>
	</Dialog>
</template>
