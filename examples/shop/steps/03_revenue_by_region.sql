-- One row per region.
SELECT region, count(*) AS orders, round(sum(amount), 2) AS revenue
FROM order_revenue
GROUP BY region
ORDER BY region
