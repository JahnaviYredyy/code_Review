# Three requests for the review tool

Use the same original `order_service.py` for all three requests. Submit each request, the actual code context sent, the raw model reply and your own short assessment. A request can be copied as plain text; there is no bug-report form.

## 1. The order amount looks wrong

> I ordered three MUGs at $10 each using pickup, no coupon. The order's subtotal is $10 and total is $11. I expected $30 before tax. Can you check what's happening?

Run from this folder using a fresh database filename:

```sh
python order_service.py --db quantity.db --at 2026-10-26T10:00:00+00:00 seed
python order_service.py --db quantity.db --at 2026-10-26T10:00:00+00:00 checkout --request cases/quantity.json
```

The actual output contains the order ID, line amounts, subtotal, tax and total. The request does not identify the faulty expression.

## 2. A cancelled order is still in the sales figures

> I placed an order, cancelled it, then ran the sales report for that day. It still shows one order and the original sales amount. Shouldn't the cancelled order be excluded? Please investigate.

Use a different fresh database. Replace `ORDER_ID` with the value printed by checkout:

```sh
python order_service.py --db cancelled.db --at 2026-10-26T10:00:00+00:00 seed
python order_service.py --db cancelled.db --at 2026-10-26T10:00:00+00:00 checkout --request cases/cancel.json
python order_service.py --db cancelled.db --at 2026-10-26T11:00:00+00:00 cancel ORDER_ID --reason "Customer changed their mind"
python order_service.py --db cancelled.db report --start 2026-10-26 --end 2026-10-26
```

The expected behavior is described in `BUSINESS_RULES.md`.

## 3. A broad request

> Review this code for bugs. Focus on mistakes that could change totals or stock. Explain the most important findings and what I should check next.

No traceback or preselected defect is supplied. Give your tool the complete original source file and this request. It must investigate without requiring the user to select functions or lines. Record the actual code context sent to the model and any limitations of the review. A confident claim about unseen code is not acceptable.

For each reply, record which findings are supported by the code, which are only possible risks, and what the model missed or assumed. Your assessment can disagree with the model. More reported bugs does not automatically mean a better review.
