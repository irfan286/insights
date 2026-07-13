<script setup lang="ts">
import { FilterOperator } from '../../types/query.types'

const filter = defineModel<{ operator: FilterOperator; value: any }>({
	type: Object,
	default: () => ({ operator: 'is_true', value: null }),
})

const operatorOptions = [
	{ label: 'is true', value: 'is_true' },
	{ label: 'is false', value: 'is_false' },
	{ label: 'is not true (false or null)', value: 'is_not_true' },
	{ label: 'is set', value: 'is_set' },
	{ label: 'is not set', value: 'is_not_set' },
]

function onOperatorChange(operator: FilterOperator) {
	filter.value.operator = operator
	filter.value.value = null
}
</script>

<template>
	<div class="flex flex-col gap-2">
		<FormControl
			type="select"
			:options="operatorOptions"
			:modelValue="filter.operator"
			@update:modelValue="onOperatorChange($event)"
		/>
	</div>
</template>
