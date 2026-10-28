# Order service rules

These rules describe the intended behavior of this exercise. The implementation may differ.
All money is in USD cents. The data is fictional and may be published with your assignment.

- Each cart line has a product SKU and a positive integer quantity. Combine repeated SKUs into one line. An empty cart is invalid.
- Merchandise subtotal is the sum of unit price multiplied by quantity for every cart line.
- A coupon reduces the merchandise subtotal. Percentages use basis points: 1,000 basis points is 10%. Respect minimum spend, maximum discount and maximum uses. A coupon remains valid throughout its expiry date in UTC. Cancelled orders do not consume coupon uses.
- Tax is 10% of merchandise after discount, rounded half up to the nearest cent. Shipping is not taxed.
- Standard shipping costs 500 cents, or is free when merchandise AFTER discount is at least 10,000 cents. Express costs 1,200 cents. Pickup is free.
- Order total is merchandise minus discount plus tax and shipping. Order line prices are snapshots: later catalog price changes must not change past orders.
- Checkout must reserve the requested quantities and create the order together, or do neither. A stock shortage must not partially reserve a cart.
- Repeating the same checkout request ID with the same input returns the existing order without reserving more stock. Different input with the same request ID is rejected.
- Cancellation is allowed before shipping. It returns reserved stock once. Repeating cancellation must not add stock again. Shipped orders cannot be cancelled.
- Only placed orders can be shipped. Tracking numbers are unique.
- Refunds apply to shipped orders and cannot exceed the amount paid across all refunds. Repeating a refund request ID must not create another refund. Refunds do not restock products automatically.
- Sales reports use the order creation date, inclusive start/end dates in UTC. Exclude cancelled orders from order counts and gross sales. Subtract all refunds associated with included orders as of the time the report is run.
- Product rankings exclude cancelled orders. They show units and merchandise amounts before discounts, tax and shipping; they are not a net-sales report.
- CSV export lists all orders in the date range, including their status. It must not overwrite an existing file.
- Inventory events record the opening quantity and subsequent changes. An internally consistent ledger alone does not prove an operation was valid.

The exercise does not specify customer authentication, real payment processing, returns logistics or deployment. Identify those as missing requirements when relevant; do not invent policies.
