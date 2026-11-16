-- Keep completed orders that have an amount.
SELECT order_id, customer_id, CAST(order_date AS DATE) AS order_date, amount
FROM orders
WHERE status = 'completed'
  AND amount IS NOT NULL
