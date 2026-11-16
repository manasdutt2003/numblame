-- Attach each order to its customer's region and segment.
SELECT o.order_id, c.region, c.segment, o.amount
FROM clean_orders AS o
JOIN customers AS c ON o.customer_id = c.customer_id
