<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <!-- Budget control -->
      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.budgetTitle') }}</h3>
          <div class="budget-value">{{ currencySymbol }}{{ budget.toLocaleString() }}</div>
        </div>
        <p class="hint">{{ t('restocking.budgetHint') }}</p>
        <input
          type="range"
          class="budget-slider"
          min="0"
          max="20000"
          step="500"
          v-model.number="budget"
        />
        <div class="slider-scale">
          <span>{{ currencySymbol }}0</span>
          <span>{{ currencySymbol }}{{ (20000).toLocaleString() }}</span>
        </div>
      </div>

      <!-- Budget allocation summary -->
      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.allocated') }}</div>
          <div class="stat-value">{{ currencySymbol }}{{ totalCost.toLocaleString() }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remaining') }}</div>
          <div class="stat-value">{{ currencySymbol }}{{ remaining.toLocaleString() }}</div>
        </div>
      </div>

      <!-- Success / error feedback after placing an order -->
      <div v-if="submitError" class="error">{{ submitError }}</div>
      <div v-if="successMessage" class="success-banner">{{ successMessage }}</div>

      <!-- Recommendations -->
      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendationsTitle') }}</h3>
          <button
            class="place-order-btn"
            :disabled="recommendations.length === 0 || placing"
            @click="placeOrder"
          >
            {{ placing ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>
        <p class="hint">{{ t('restocking.recommendationsHint') }}</p>

        <div v-if="recommendations.length === 0" class="empty-state">
          {{ t('restocking.noRecommendations') }}
        </div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.itemName') }}</th>
                <th>{{ t('restocking.table.demandGap') }}</th>
                <th>{{ t('restocking.table.quantity') }}</th>
                <th>{{ t('restocking.table.unitCost') }}</th>
                <th>{{ t('restocking.table.lineTotal') }}</th>
                <th>{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in recommendations" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td>{{ item.gap.toLocaleString() }}</td>
                <td><strong>{{ item.quantity.toLocaleString() }}</strong></td>
                <td>{{ currencySymbol }}{{ item.unit_cost.toLocaleString() }}</td>
                <td>{{ currencySymbol }}{{ item.line_total.toLocaleString() }}</td>
                <td>{{ t('restocking.leadTimeDays', { days: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
            <tfoot>
              <tr class="totals-row">
                <td colspan="5">{{ t('restocking.itemsRecommended', { count: recommendations.length }) }}</td>
                <td colspan="2"><strong>{{ currencySymbol }}{{ totalCost.toLocaleString() }}</strong></td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const currencySymbol = computed(() => currentCurrency.value === 'JPY' ? '¥' : '$')

    const loading = ref(true)
    const error = ref(null)
    const forecasts = ref([])

    // Budget the user is willing to spend on restocking (driven by the slider).
    const budget = ref(8000)

    const placing = ref(false)
    const submitError = ref(null)
    const successMessage = ref(null)

    const loadForecasts = async () => {
      try {
        loading.value = true
        forecasts.value = await api.getDemandForecasts()
      } catch (err) {
        error.value = 'Failed to load demand forecasts: ' + err.message
      } finally {
        loading.value = false
      }
    }

    // Greedy recommendation: spend the budget on the items with the largest
    // demand gaps first. Each item is filled up to its full gap, except the last
    // affordable item which is partially filled so the budget is used as fully as
    // possible. Recomputes reactively as the budget slider moves.
    const recommendations = computed(() => {
      const candidates = forecasts.value
        .map(f => ({ ...f, gap: Math.max(0, f.forecasted_demand - f.current_demand) }))
        .filter(f => f.gap > 0)
        // Largest gap first; SKU as a stable tiebreaker for deterministic ordering.
        .sort((a, b) => b.gap - a.gap || a.item_sku.localeCompare(b.item_sku))

      let remainingBudget = budget.value
      const picks = []
      for (const c of candidates) {
        if (remainingBudget < c.unit_cost) continue
        const maxAffordable = Math.floor(remainingBudget / c.unit_cost)
        const quantity = Math.min(c.gap, maxAffordable)
        if (quantity <= 0) continue
        const line_total = quantity * c.unit_cost
        picks.push({
          sku: c.item_sku,
          name: c.item_name,
          gap: c.gap,
          quantity,
          unit_cost: c.unit_cost,
          line_total,
          lead_time_days: c.lead_time_days
        })
        remainingBudget -= line_total
      }
      return picks
    })

    const totalCost = computed(() =>
      recommendations.value.reduce((sum, item) => sum + item.line_total, 0)
    )

    const remaining = computed(() => budget.value - totalCost.value)

    const placeOrder = async () => {
      if (recommendations.value.length === 0) return
      try {
        placing.value = true
        submitError.value = null
        successMessage.value = null

        const payload = {
          budget: budget.value,
          total_value: totalCost.value,
          items: recommendations.value.map(r => ({
            sku: r.sku,
            name: r.name,
            quantity: r.quantity,
            unit_cost: r.unit_cost,
            line_total: r.line_total,
            lead_time_days: r.lead_time_days
          }))
        }

        const result = await api.createRestockingOrder(payload)
        successMessage.value = t('restocking.orderSuccess', {
          orderNumber: result.order_number,
          days: result.lead_time_days
        })
      } catch (err) {
        submitError.value = t('restocking.orderError')
        console.error('Failed to place restocking order:', err)
      } finally {
        placing.value = false
      }
    }

    onMounted(loadForecasts)

    return {
      t,
      currencySymbol,
      loading,
      error,
      budget,
      recommendations,
      totalCost,
      remaining,
      placing,
      submitError,
      successMessage,
      placeOrder,
      translateProductName
    }
  }
}
</script>

<style scoped>
.hint {
  color: #64748b;
  font-size: 0.875rem;
  margin-bottom: 1rem;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #2563eb;
  letter-spacing: -0.025em;
}

.budget-slider {
  width: 100%;
  height: 6px;
  border-radius: 3px;
  background: #e2e8f0;
  outline: none;
  -webkit-appearance: none;
  appearance: none;
  cursor: pointer;
}

.budget-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #2563eb;
  border: 2px solid #ffffff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  cursor: pointer;
}

.budget-slider::-moz-range-thumb {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #2563eb;
  border: 2px solid #ffffff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
  cursor: pointer;
}

.slider-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 0.5rem;
  font-size: 0.75rem;
  color: #94a3b8;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
  font-size: 0.938rem;
  background: #f8fafc;
  border-radius: 8px;
}

.totals-row td {
  border-top: 2px solid #e2e8f0;
  font-size: 0.938rem;
  color: #0f172a;
}

.place-order-btn {
  background: #2563eb;
  color: #ffffff;
  border: none;
  padding: 0.625rem 1.25rem;
  border-radius: 6px;
  font-size: 0.938rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s ease;
}

.place-order-btn:hover:not(:disabled) {
  background: #1d4ed8;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.success-banner {
  background: #d1fae5;
  border: 1px solid #6ee7b7;
  color: #065f46;
  padding: 1rem;
  border-radius: 8px;
  margin: 1rem 0;
  font-size: 0.938rem;
  font-weight: 500;
}
</style>
