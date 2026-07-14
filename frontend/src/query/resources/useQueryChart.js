import { areDeeplyEqual, createTaskRunner, safeJSONParse } from '@/utils'
import { convertResultToObjects, guessChart } from '@/widgets/useChartData'
import { watchDebounced } from '@vueuse/core'
import { call, createDocumentResource } from 'frappe-ui'
import { computed, reactive, ref, watch } from 'vue'

export default function useQueryChart(chartName, queryTitle, queryResults) {
	const resource = getChartResource(chartName)
	resource.get.fetch()

	// Local editing state — never overwritten by server responses.
	// resource.doc is only used as the server-synced baseline (for dirty detection
	// via resource.originalDoc). All UI mutations go through these local refs so
	// that in-flight saves don't clobber the user's current edits.
	const localOptions = ref(null)
	const localChartType = ref(null)

	// Initialise local state once when the server doc first arrives.
	watch(
		() => resource.doc,
		(doc) => {
			if (doc && localOptions.value === null) {
				localChartType.value = doc.chart_type
				localOptions.value = doc.options ? JSON.parse(JSON.stringify(doc.options)) : {}
			}
		},
		{ immediate: true }
	)

	// chart.doc exposes the merged view: local editing state for options/chart_type,
	// passthrough to resource.doc for all other server-managed fields.
	// The getter/setter property descriptors make v-model and direct assignments
	// (e.g. chart.doc.options = {}) update localOptions/localChartType correctly.
	const chart = reactive({
		doc: computed(() => {
			const base = resource.doc || {}
			return {
				...base,
				get chart_type() {
					return localChartType.value ?? base.chart_type
				},
				set chart_type(v) {
					localChartType.value = v
				},
				get options() {
					return localOptions.value ?? base.options
				},
				set options(v) {
					localOptions.value = v
				},
			}
		}),
		data: computed(() => convertResultToObjects(queryResults.formattedResults)),
		togglePublicAccess,
		addToDashboard,
		getGuessedChart,
		resetOptions,
		delete: deleteChart,
	})

	const run = createTaskRunner()
	// Watch local refs so server responses (which overwrite resource.doc) never
	// accidentally trigger or suppress a save.
	watchDebounced(
		() => ({
			chart_type: localChartType.value,
			options: localOptions.value,
		}),
		_updateDoc,
		{ deep: true, debounce: 1000 }
	)

	async function _updateDoc(newDoc) {
		const ogDoc = resource.originalDoc
		if (!ogDoc) return
		const chartTypeChanged = newDoc.chart_type != ogDoc.chart_type
		const optionsChanged = !areDeeplyEqual(newDoc.options, ogDoc.options)
		if (!chartTypeChanged && !optionsChanged) return

		let newOptions = { ...newDoc.options }
		if (!newOptions.query) {
			newOptions.query = resource.doc?.query
		}

		if (chartTypeChanged && newDoc.chart_type != 'Auto') {
			const guessedChart = getGuessedChart(newDoc.chart_type)
			newOptions = { ...guessedChart.options, ...newOptions }
			newOptions.title = queryTitle
		}
		_save({ chart_type: newDoc.chart_type, options: newOptions })
	}

	function _save(chartDoc) {
		chartDoc.options.query = resource.doc?.query
		return run(() =>
			resource.setValue.submit({
				title: chartDoc.options.title || queryTitle,
				chart_type: chartDoc.chart_type,
				options: chartDoc.options,
			})
		)
	}

	function getGuessedChart(chart_type) {
		if (!queryResults.formattedResults.length) return
		chart_type = chart_type || localChartType.value
		const recommendedChart = guessChart(queryResults.formattedResults, chart_type)
		return {
			chart_type: recommendedChart?.type,
			options: {
				...recommendedChart?.options,
				title: recommendedChart?.options?.title || queryTitle,
			},
		}
	}

	function togglePublicAccess(isPublic) {
		if (resource.doc.is_public === isPublic) return
		resource.setValue.submit({ is_public: isPublic }).then(() => {
			$notify({
				title: 'Chart access updated',
				variant: 'success',
			})
			resource.doc.is_public = isPublic
		})
	}

	async function addToDashboard(dashboardName) {
		if (!dashboardName || !resource.doc.name || resource.addingToDashboard) return
		resource.addingToDashboard = true
		if (localChartType.value == 'Auto') {
			const guessedChart = getGuessedChart()
			await _save({
				chart_type: guessedChart.chart_type,
				options: guessedChart.options,
			})
		}
		await call('insights.api.dashboards.add_chart_to_dashboard', {
			dashboard: dashboardName,
			chart: resource.doc.name,
		})
		resource.addingToDashboard = false
	}

	function resetOptions() {
		localChartType.value = undefined
		localOptions.value = {}
	}

	async function deleteChart() {
		return run(() => resource.delete.submit())
	}

	return chart
}

export function getChartResource(chartName) {
	return createDocumentResource({
		doctype: 'Insights Chart',
		name: chartName,
		auto: false,
		transform: (doc) => {
			doc.chart_type = doc.chart_type
			doc.options = safeJSONParse(doc.options)
			return doc
		},
	})
}
