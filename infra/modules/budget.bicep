// Subscription budget with actual-cost alerts at 25, 50, 75 and 90 percent.
targetScope = 'subscription'

@description('Monthly budget in USD.')
param budgetAmount int

@description('Notification address. Passed at deploy time, never committed.')
param budgetEmail string

@description('First day of the month in which the budget starts (yyyy-MM-dd).')
param startDate string

var thresholds = [
  25
  50
  75
  90
]

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-cfdagent'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: toObject(
      thresholds,
      t => 'actual-${t}-percent',
      t => {
        enabled: true
        operator: 'GreaterThan'
        threshold: t
        thresholdType: 'Actual'
        contactEmails: [
          budgetEmail
        ]
      }
    )
  }
}
