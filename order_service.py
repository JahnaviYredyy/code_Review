"""Local order-processing service for the Week 1 code review assignment.

Python 3.11 or newer; standard library only. All customers and products are
fictional. Money is stored in integer cents. No network or payment provider is
used. Commands operate on a local SQLite file selected with --db.

Examples:
    python order_service.py --db practice.db seed
    python order_service.py --db practice.db catalog
    python order_service.py --db practice.db checkout --request cases/quantity.json
    python order_service.py --db practice.db report --start 2026-01-01 --end 2026-12-31

Read BUSINESS_RULES.md for the intended behavior. This file is supplied for
inspection, not as a correct reference implementation or a production service.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

LOG = logging.getLogger("order_service")
SCHEMA_VERSION = 1
MAX_CART_LINES = 40
MAX_LINE_QUANTITY = 100
TAX_BASIS_POINTS = 1000
FREE_SHIPPING_CENTS = 10000
SHIPPING_CENTS = {"standard": 500, "express": 1200, "pickup": 0}
VALID_STATUSES = {"placed", "shipped", "cancelled"}


class ServiceError(Exception):
    """An input or business-rule error suitable for a command-line user."""


class NotFound(ServiceError):
    """A requested customer, product or order does not exist."""


class Conflict(ServiceError):
    """The operation conflicts with current stock or order state."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ServiceError("Use an ISO timestamp with a timezone") from exc
    if result.tzinfo is None:
        raise ServiceError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ServiceError("Cannot store a timestamp without a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ServiceError("Dates must use YYYY-MM-DD") from exc


def nonempty(value: Any, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(f"{name} must be a nonempty string")
    result = value.strip()
    if len(result) > maximum:
        raise ServiceError(f"{name} exceeds {maximum} characters")
    return result


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ServiceError(f"{name} must be between {minimum} and {maximum}")
    return value


def money_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ServiceError("Invalid money amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ServiceError("Money amount must be finite and nonnegative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def percentage(cents: int, basis_points: int) -> int:
    return int((Decimal(cents) * basis_points / 10000).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    ))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 100_000:
        raise ServiceError("Request file exceeds 100000 bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ServiceError("Request must be a UTF-8 JSON document") from exc
    if not isinstance(value, dict):
        raise ServiceError("Request must contain a JSON object")
    return value


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    region: str
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Customer:
        return cls(row["customer_id"], row["name"], row["region"], bool(row["active"]))


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    price_cents: int
    stock: int
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Product:
        return cls(row["sku"], row["name"], row["category"], row["price_cents"],
                   row["stock"], bool(row["active"]))


@dataclass(frozen=True)
class CartLine:
    sku: str
    quantity: int

    @classmethod
    def from_dict(cls, value: Any) -> CartLine:
        if not isinstance(value, dict):
            raise ServiceError("Each cart line must be an object")
        sku = nonempty(value.get("sku"), "sku", 40).upper()
        quantity = integer(value.get("quantity"), "quantity", 1, MAX_LINE_QUANTITY)
        return cls(sku, quantity)


@dataclass(frozen=True)
class CheckoutRequest:
    customer_id: str
    lines: tuple[CartLine, ...]
    shipping: str
    coupon: str | None
    request_id: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CheckoutRequest:
        customer_id = nonempty(value.get("customer_id"), "customer_id", 40)
        request_id = nonempty(value.get("request_id"), "request_id", 80)
        raw_lines = value.get("items")
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_CART_LINES:
            raise ServiceError(f"Cart must contain 1 to {MAX_CART_LINES} lines")
        lines = tuple(CartLine.from_dict(item) for item in raw_lines)
        if len({line.sku for line in lines}) != len(lines):
            raise ServiceError("Combine duplicate SKUs into one cart line")
        shipping = value.get("shipping", "standard")
        if not isinstance(shipping, str) or shipping not in SHIPPING_CENTS:
            raise ServiceError("Shipping must be standard, express or pickup")
        coupon = value.get("coupon")
        if coupon is not None:
            coupon = nonempty(coupon, "coupon", 40).upper()
        return cls(customer_id, lines, shipping, coupon, request_id)

    def fingerprint(self) -> str:
        return payload_hash(asdict(self))


@dataclass(frozen=True)
class PricedLine:
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int


@dataclass(frozen=True)
class Quote:
    lines: tuple[PricedLine, ...]
    subtotal_cents: int
    discount_cents: int
    shipping_cents: int
    tax_cents: int
    total_cents: int
    coupon: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["formatted_total"] = format_money(self.total_cents)
        return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    stock INTEGER NOT NULL CHECK(stock >= 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    basis_points INTEGER NOT NULL CHECK(basis_points BETWEEN 0 AND 10000),
    minimum_cents INTEGER NOT NULL CHECK(minimum_cents >= 0),
    max_discount_cents INTEGER NOT NULL CHECK(max_discount_cents >= 0),
    expires_on TEXT NOT NULL,
    max_uses INTEGER NOT NULL CHECK(max_uses > 0),
    active INTEGER NOT NULL CHECK(active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('placed', 'shipped', 'cancelled')),
    shipping_method TEXT NOT NULL,
    coupon TEXT,
    subtotal_cents INTEGER NOT NULL,
    discount_cents INTEGER NOT NULL,
    shipping_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_lines (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    sku TEXT NOT NULL REFERENCES products(sku),
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price_cents INTEGER NOT NULL,
    line_total_cents INTEGER NOT NULL,
    PRIMARY KEY(order_id, sku)
);
CREATE TABLE IF NOT EXISTS inventory_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL REFERENCES products(sku),
    order_id TEXT REFERENCES orders(order_id),
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
    tracking_number TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    shipped_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    request_id TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory_events(sku);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
"""


class Database:
    def __init__(self, path: Path):
        if not path.parent.is_dir():
            raise ServiceError("Database directory does not exist")
        self.path = path
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        current = self.one("SELECT value FROM metadata WHERE key = ?", ("schema",))
        if current and int(current["value"]) != SCHEMA_VERSION:
            raise Conflict("Unsupported database schema version")
        self.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES (?, ?)",
                     ("schema", str(SCHEMA_VERSION)))

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, parameters)

    def one(self, sql: str, parameters: tuple = ()) -> sqlite3.Row | None:
        return self.execute(sql, parameters).fetchone()

    def all(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        return self.execute(sql, parameters).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.execute("ROLLBACK")
            raise
        else:
            self.execute("COMMIT")

    def audit(self, kind: str, entity_id: str, action: str,
              detail: dict[str, Any], now: datetime) -> None:
        self.execute(
            "INSERT INTO audit_events(entity_type, entity_id, action, detail_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, entity_id, action, canonical_json(detail), timestamp(now)),
        )

    def customer(self, customer_id: str) -> Customer:
        row = self.one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
        if row is None:
            raise NotFound(f"Unknown customer: {customer_id}")
        return Customer.from_row(row)

    def product(self, sku: str) -> Product:
        row = self.one("SELECT * FROM products WHERE sku = ?", (sku,))
        if row is None:
            raise NotFound(f"Unknown SKU: {sku}")
        return Product.from_row(row)

    def order(self, order_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        if row is None:
            raise NotFound(f"Unknown order: {order_id}")
        return row

    def lines(self, order_id: str) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM order_lines WHERE order_id = ? ORDER BY sku",
                        (order_id,))

    def inventory_event(self, sku: str, delta: int, reason: str,
                        now: datetime, order_id: str | None = None) -> None:
        self.execute(
            "INSERT INTO inventory_events(sku, order_id, delta, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sku, order_id, delta, reason, timestamp(now)),
        )

    def refund_total(self, order_id: str) -> int:
        row = self.one("SELECT COALESCE(SUM(amount_cents), 0) AS amount "
                       "FROM refunds WHERE order_id = ?", (order_id,))
        return int(row["amount"])


class CatalogService:
    def __init__(self, db: Database):
        self.db = db

    def list_products(self, category: str | None = None,
                      include_inactive: bool = False) -> list[dict[str, Any]]:
        conditions = []
        values: list[Any] = []
        if not include_inactive:
            conditions.append("active = 1")
        if category:
            conditions.append("category = ?")
            values.append(category)
        sql = "SELECT * FROM products"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY category, sku"
        return [dict(row) for row in self.db.all(sql, tuple(values))]

    def update_price(self, sku: str, price_cents: int, now: datetime) -> dict:
        integer(price_cents, "price_cents", 0, 10_000_000)
        with self.db.transaction():
            product = self.db.product(sku)
            self.db.execute("UPDATE products SET price_cents = ?, updated_at = ? "
                            "WHERE sku = ?", (price_cents, timestamp(now), sku))
            self.db.audit("product", sku, "price_changed",
                          {"old": product.price_cents, "new": price_cents}, now)
        return asdict(self.db.product(sku))

    def set_active(self, sku: str, active: bool, now: datetime) -> dict:
        with self.db.transaction():
            self.db.product(sku)
            self.db.execute("UPDATE products SET active = ?, updated_at = ? WHERE sku = ?",
                            (int(active), timestamp(now), sku))
            self.db.audit("product", sku, "availability_changed", {"active": active}, now)
        return asdict(self.db.product(sku))

    def restock(self, sku: str, quantity: int, reason: str, now: datetime) -> dict:
        integer(quantity, "quantity", 1, 100_000)
        reason = nonempty(reason, "reason", 200)
        with self.db.transaction():
            self.db.product(sku)
            self.db.execute("UPDATE products SET stock = stock + ?, updated_at = ? "
                            "WHERE sku = ?", (quantity, timestamp(now), sku))
            self.db.inventory_event(sku, quantity, reason, now)
            self.db.audit("product", sku, "restocked", {"quantity": quantity}, now)
        return asdict(self.db.product(sku))

    def stock_history(self, sku: str, limit: int = 50) -> list[dict]:
        integer(limit, "limit", 1, 500)
        self.db.product(sku)
        rows = self.db.all("SELECT * FROM inventory_events WHERE sku = ? "
                           "ORDER BY event_id DESC LIMIT ?", (sku, limit))
        return [dict(row) for row in rows]

    def low_stock(self, threshold: int) -> list[dict]:
        integer(threshold, "threshold", 0, 100_000)
        rows = self.db.all("SELECT sku, name, stock FROM products "
                           "WHERE active = 1 AND stock <= ? ORDER BY stock, sku",
                           (threshold,))
        return [dict(row) for row in rows]


class PricingService:
    def __init__(self, db: Database):
        self.db = db

    def price_lines(self, lines: tuple[CartLine, ...]) -> tuple[PricedLine, ...]:
        result = []
        for line in lines:
            product = self.db.product(line.sku)
            if not product.active:
                raise Conflict(f"Product is unavailable: {line.sku}")
            if product.stock < line.quantity:
                raise Conflict(f"Insufficient stock for {line.sku}")
            result.append(PricedLine(
                sku=product.sku,
                name=product.name,
                quantity=line.quantity,
                unit_price_cents=product.price_cents,
                line_total_cents=product.price_cents * line.quantity,
            ))
        return tuple(result)

    def coupon_discount(self, code: str | None, subtotal: int, now: datetime) -> int:
        if code is None:
            return 0
        row = self.db.one("SELECT * FROM coupons WHERE code = ?", (code,))
        if row is None or not row["active"]:
            raise Conflict("Coupon is not available")
        expires = parse_day(row["expires_on"])
        if now.date() > expires:
            raise Conflict("Coupon has expired")
        if subtotal < row["minimum_cents"]:
            raise Conflict("Order does not meet coupon minimum")
        usage = self.db.one("SELECT COUNT(*) AS count FROM orders "
                            "WHERE coupon = ? AND status != 'cancelled'", (code,))
        if usage["count"] >= row["max_uses"]:
            raise Conflict("Coupon usage limit reached")
        discount = percentage(subtotal, row["basis_points"])
        return min(discount, row["max_discount_cents"], subtotal)

    def shipping_cost(self, method: str, subtotal: int, discount: int) -> int:
        if method == "pickup":
            return 0
        if method == "standard" and subtotal >= FREE_SHIPPING_CENTS:
            return 0
        return SHIPPING_CENTS[method]

    def quote(self, request: CheckoutRequest, now: datetime) -> Quote:
        customer = self.db.customer(request.customer_id)
        if not customer.active:
            raise Conflict("Customer account is inactive")
        lines = self.price_lines(request.lines)
        subtotal = sum(line.unit_price_cents for line in lines)
        discount = self.coupon_discount(request.coupon, subtotal, now)
        shipping = self.shipping_cost(request.shipping, subtotal, discount)
        taxable = max(subtotal - discount, 0)
        tax = percentage(taxable, TAX_BASIS_POINTS)
        return Quote(
            lines=lines,
            subtotal_cents=subtotal,
            discount_cents=discount,
            shipping_cents=shipping,
            tax_cents=tax,
            total_cents=taxable + tax + shipping,
            coupon=request.coupon,
        )


class OrderService:
    def __init__(self, db: Database):
        self.db = db
        self.pricing = PricingService(db)

    def view(self, order_id: str) -> dict[str, Any]:
        result = dict(self.db.order(order_id))
        result.pop("request_hash")
        result["items"] = [dict(row) for row in self.db.lines(order_id)]
        result["refunded_cents"] = self.db.refund_total(order_id)
        result["formatted_total"] = format_money(result["total_cents"])
        shipment = self.db.one("SELECT * FROM shipments WHERE order_id = ?", (order_id,))
        result["shipment"] = dict(shipment) if shipment else None
        return result

    def list_orders(self, customer_id: str | None = None,
                    status: str | None = None, limit: int = 50) -> list[dict]:
        integer(limit, "limit", 1, 500)
        clauses = []
        values: list[Any] = []
        if customer_id:
            self.db.customer(customer_id)
            clauses.append("customer_id = ?")
            values.append(customer_id)
        if status:
            if status not in VALID_STATUSES:
                raise ServiceError("Unknown order status")
            clauses.append("status = ?")
            values.append(status)
        sql = "SELECT order_id, customer_id, status, total_cents, created_at FROM orders"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, order_id LIMIT ?"
        values.append(limit)
        return [dict(row) for row in self.db.all(sql, tuple(values))]

    def checkout(self, request: CheckoutRequest, now: datetime) -> dict:
        with self.db.transaction():
            existing = self.db.one("SELECT * FROM orders WHERE request_id = ?",
                                   (request.request_id,))
            if existing:
                if existing["request_hash"] != request.fingerprint():
                    raise Conflict("Request ID was already used with different input")
                order_id = existing["order_id"]
            else:
                quote = self.pricing.quote(request, now)
                order_id = "ORD-" + payload_hash(request.request_id)[:12].upper()
                self._insert_order(order_id, request, quote, now)
                for line in quote.lines:
                    self._reserve_line(order_id, line, now)
                self.db.audit("order", order_id, "placed", quote.to_dict(), now)
        return self.view(order_id)

    def _insert_order(self, order_id: str, request: CheckoutRequest,
                      quote: Quote, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO orders(order_id, customer_id, request_id, request_hash, status, "
            "shipping_method, coupon, subtotal_cents, discount_cents, shipping_cents, "
            "tax_cents, total_cents, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, request.customer_id, request.request_id, request.fingerprint(),
             "placed", request.shipping, quote.coupon, quote.subtotal_cents,
             quote.discount_cents, quote.shipping_cents, quote.tax_cents,
             quote.total_cents, timestamp(now), timestamp(now)),
        )
        for line in quote.lines:
            self.db.execute(
                "INSERT INTO order_lines(order_id, sku, name, quantity, "
                "unit_price_cents, line_total_cents) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, line.sku, line.name, line.quantity,
                 line.unit_price_cents, line.line_total_cents),
            )

    def _reserve_line(self, order_id: str, line: PricedLine, now: datetime) -> None:
        changed = self.db.execute(
            "UPDATE products SET stock = stock - ?, updated_at = ? "
            "WHERE sku = ? AND active = 1 AND stock >= ?",
            (line.quantity, timestamp(now), line.sku, line.quantity),
        ).rowcount
        if changed != 1:
            raise Conflict(f"Stock changed while placing order: {line.sku}")
        self.db.inventory_event(line.sku, -line.quantity, "checkout", now, order_id)

    def cancel(self, order_id: str, reason: str, now: datetime) -> dict:
        reason = nonempty(reason, "reason", 200)
        with self.db.transaction():
            order = self.db.order(order_id)
            if order["status"] == "shipped":
                raise Conflict("A shipped order cannot be cancelled")
            for line in self.db.lines(order_id):
                self.db.execute("UPDATE products SET stock = stock + ?, updated_at = ? "
                                "WHERE sku = ?",
                                (line["quantity"], timestamp(now), line["sku"]))
                self.db.inventory_event(line["sku"], line["quantity"],
                                        "cancellation", now, order_id)
            self.db.execute("UPDATE orders SET status = 'cancelled', updated_at = ? "
                            "WHERE order_id = ?", (timestamp(now), order_id))
            self.db.audit("order", order_id, "cancelled", {"reason": reason}, now)
        return self.view(order_id)

    def ship(self, order_id: str, carrier: str, tracking: str, now: datetime) -> dict:
        carrier = nonempty(carrier, "carrier", 80)
        tracking = nonempty(tracking, "tracking", 80)
        with self.db.transaction():
            order = self.db.order(order_id)
            if order["status"] != "placed":
                raise Conflict("Only placed orders can be shipped")
            duplicate = self.db.one("SELECT order_id FROM shipments WHERE tracking_number = ?",
                                    (tracking,))
            if duplicate:
                raise Conflict("Tracking number is already assigned")
            self.db.execute("INSERT INTO shipments(order_id, tracking_number, carrier, "
                            "shipped_at) VALUES (?, ?, ?, ?)",
                            (order_id, tracking, carrier, timestamp(now)))
            self.db.execute("UPDATE orders SET status = 'shipped', updated_at = ? "
                            "WHERE order_id = ?", (timestamp(now), order_id))
            self.db.audit("order", order_id, "shipped",
                          {"carrier": carrier, "tracking": tracking}, now)
        return self.view(order_id)

    def refund(self, order_id: str, amount_cents: int, request_id: str,
               reason: str, now: datetime) -> dict:
        integer(amount_cents, "amount_cents", 1, 100_000_000)
        request_id = nonempty(request_id, "request_id", 80)
        reason = nonempty(reason, "reason", 200)
        with self.db.transaction():
            existing = self.db.one("SELECT * FROM refunds WHERE request_id = ?", (request_id,))
            if existing:
                if existing["order_id"] != order_id or existing["amount_cents"] != amount_cents:
                    raise Conflict("Refund request ID is already in use")
                return dict(existing)
            order = self.db.order(order_id)
            if order["status"] != "shipped":
                raise Conflict("Only shipped orders can be refunded")
            remaining = order["total_cents"] - self.db.refund_total(order_id)
            if amount_cents > remaining:
                raise Conflict("Refund would exceed the order total")
            refund_id = "REF-" + payload_hash(request_id)[:12].upper()
            self.db.execute("INSERT INTO refunds(refund_id, order_id, request_id, "
                            "amount_cents, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (refund_id, order_id, request_id, amount_cents, reason, timestamp(now)))
            self.db.audit("order", order_id, "refunded",
                          {"refund_id": refund_id, "amount_cents": amount_cents}, now)
        return dict(self.db.one("SELECT * FROM refunds WHERE refund_id = ?", (refund_id,)))

    def history(self, order_id: str) -> list[dict]:
        self.db.order(order_id)
        rows = self.db.all("SELECT * FROM audit_events WHERE entity_type = 'order' "
                           "AND entity_id = ? ORDER BY event_id", (order_id,))
        result = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event.pop("detail_json"))
            result.append(event)
        return result


class ReportingService:
    def __init__(self, db: Database):
        self.db = db

    def date_bounds(self, start: str, end: str) -> tuple[str, str]:
        first = parse_day(start)
        last = parse_day(end)
        if first > last:
            raise ServiceError("Start date must not be after end date")
        if (last - first).days > 366:
            raise ServiceError("Report range must not exceed 367 days")
        after = last + timedelta(days=1)
        return first.isoformat() + "T00:00:00+00:00", after.isoformat() + "T00:00:00+00:00"

    def sales(self, start: str, end: str) -> dict:
        lower, upper = self.date_bounds(start, end)
        row = self.db.one(
            "SELECT COUNT(*) AS order_count, COALESCE(SUM(total_cents), 0) AS gross_cents, "
            "COALESCE(SUM(discount_cents), 0) AS discount_cents, "
            "COALESCE(SUM(tax_cents), 0) AS tax_cents, "
            "COALESCE(SUM(shipping_cents), 0) AS shipping_cents "
            "FROM orders WHERE created_at >= ? AND created_at < ?",
            (lower, upper),
        )
        refunds = self.db.one(
            "SELECT COALESCE(SUM(r.amount_cents), 0) AS refunded_cents "
            "FROM refunds r JOIN orders o ON o.order_id = r.order_id "
            "WHERE o.created_at >= ? AND o.created_at < ?",
            (lower, upper),
        )
        result = dict(row)
        result.update(dict(refunds))
        result["net_cents"] = result["gross_cents"] - result["refunded_cents"]
        result["start"] = start
        result["end"] = end
        count = result["order_count"]
        result["average_order_cents"] = round(result["gross_cents"] / count) if count else 0
        result["currency"] = "USD"
        return result

    def status_counts(self) -> dict[str, int]:
        rows = self.db.all("SELECT status, COUNT(*) AS count FROM orders GROUP BY status")
        result = {status: 0 for status in sorted(VALID_STATUSES)}
        result.update({row["status"]: row["count"] for row in rows})
        return result

    def top_products(self, start: str, end: str, limit: int = 10) -> list[dict]:
        integer(limit, "limit", 1, 100)
        lower, upper = self.date_bounds(start, end)
        rows = self.db.all(
            "SELECT l.sku, l.name, SUM(l.quantity) AS units, "
            "SUM(l.line_total_cents) AS merchandise_cents "
            "FROM order_lines l JOIN orders o ON o.order_id = l.order_id "
            "WHERE o.created_at >= ? AND o.created_at < ? AND o.status != 'cancelled' "
            "GROUP BY l.sku, l.name ORDER BY units DESC, l.sku LIMIT ?",
            (lower, upper, limit),
        )
        return [dict(row) for row in rows]

    def customer_summary(self, customer_id: str) -> dict:
        customer = self.db.customer(customer_id)
        row = self.db.one(
            "SELECT COUNT(*) AS count, COALESCE(SUM(total_cents), 0) AS total "
            "FROM orders WHERE customer_id = ? AND status != 'cancelled'",
            (customer_id,),
        )
        return {"customer": asdict(customer), "order_count": row["count"],
                "ordered_cents": row["total"]}

    def inventory_value(self) -> dict:
        rows = self.db.all("SELECT category, SUM(stock) AS units, "
                           "SUM(stock * price_cents) AS value_cents FROM products "
                           "GROUP BY category ORDER BY category")
        categories = [dict(row) for row in rows]
        return {"categories": categories,
                "total_units": sum(row["units"] for row in categories),
                "total_value_cents": sum(row["value_cents"] for row in categories)}

    def reconcile_inventory(self) -> list[dict]:
        rows = self.db.all(
            "SELECT p.sku, p.stock, COALESCE(SUM(e.delta), 0) AS ledger_stock "
            "FROM products p LEFT JOIN inventory_events e ON e.sku = p.sku "
            "GROUP BY p.sku, p.stock ORDER BY p.sku",
        )
        return [{"sku": row["sku"], "stock": row["stock"],
                 "ledger_stock": row["ledger_stock"],
                 "matches": row["stock"] == row["ledger_stock"]} for row in rows]

    def export_orders(self, path: Path, start: str, end: str) -> dict:
        lower, upper = self.date_bounds(start, end)
        if not path.parent.is_dir():
            raise ServiceError("Export directory does not exist")
        rows = self.db.all(
            "SELECT order_id, customer_id, status, subtotal_cents, discount_cents, "
            "shipping_cents, tax_cents, total_cents, created_at FROM orders "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at, order_id",
            (lower, upper),
        )
        columns = ["order_id", "customer_id", "status", "subtotal_cents", "discount_cents",
                   "shipping_cents", "tax_cents", "total_cents", "created_at"]
        with path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return {"path": str(path), "row_count": len(rows)}

    def health(self) -> dict:
        integrity = self.db.one("PRAGMA integrity_check")[0]
        foreign_key_errors = self.db.all("PRAGMA foreign_key_check")
        stock = self.reconcile_inventory()
        return {
            "schema_version": SCHEMA_VERSION,
            "sqlite_integrity": integrity,
            "foreign_key_errors": len(foreign_key_errors),
            "inventory_mismatches": [item for item in stock if not item["matches"]],
            "status_counts": self.status_counts(),
        }


DEMO_CUSTOMERS = (
    ("C-100", "Northwind Workshop", "north", 1),
    ("C-200", "Riverbank Studio", "south", 1),
    ("C-300", "Closed Demo Account", "north", 0),
)
DEMO_PRODUCTS = (
    ("MUG", "Ceramic mug", "home", 1000, 30, 1),
    ("BOOK", "Plain notebook", "office", 1500, 40, 1),
    ("BAG", "Canvas bag", "travel", 4500, 12, 1),
    ("LAMP", "Desk lamp", "home", 6500, 8, 1),
    ("PEN", "Blue pen", "office", 250, 100, 1),
    ("OLD", "Retired sample", "office", 500, 5, 0),
)
DEMO_COUPONS = (
    ("SAVE10", 1000, 1000, 5000, "2026-12-31", 100, 1),
    ("HALF", 5000, 10000, 20000, "2026-12-31", 5, 1),
    ("EXPIRED", 1000, 0, 5000, "2025-12-31", 100, 1),
)


def seed_demo(db: Database, now: datetime) -> dict:
    with db.transaction():
        if db.one("SELECT value FROM metadata WHERE key = 'seeded'"):
            return {"seeded": False, "message": "Demo data already exists"}
        for customer in DEMO_CUSTOMERS:
            db.execute("INSERT INTO customers(customer_id, name, region, active) "
                       "VALUES (?, ?, ?, ?)", customer)
        for product in DEMO_PRODUCTS:
            db.execute("INSERT INTO products(sku, name, category, price_cents, stock, "
                       "active, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       product + (timestamp(now),))
            db.inventory_event(product[0], product[4], "opening_stock", now)
        for coupon in DEMO_COUPONS:
            db.execute("INSERT INTO coupons(code, basis_points, minimum_cents, "
                       "max_discount_cents, expires_on, max_uses, active) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", coupon)
        db.execute("INSERT INTO metadata(key, value) VALUES ('seeded', ?)", (timestamp(now),))
        db.audit("database", "demo", "seeded", {"products": len(DEMO_PRODUCTS)}, now)
    return {"seeded": True, "customers": len(DEMO_CUSTOMERS),
            "products": len(DEMO_PRODUCTS), "coupons": len(DEMO_COUPONS)}


def import_orders(db: Database, path: Path, now: datetime) -> dict:
    if path.stat().st_size > 1_000_000:
        raise ServiceError("Import exceeds 1000000 bytes")
    service = OrderService(db)
    accepted = []
    rejected = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if number > 1000:
                rejected.append({"line": number, "error": "Import line limit reached"})
                break
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ServiceError("Each line must be an object")
                request = CheckoutRequest.from_dict(value)
                order = service.checkout(request, now)
                accepted.append({"line": number, "order_id": order["order_id"]})
            except (ServiceError, json.JSONDecodeError) as exc:
                rejected.append({"line": number, "error": str(exc)})
    return {"accepted": accepted, "rejected": rejected}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local demo order-processing service")
    parser.add_argument("--db", type=Path, default=Path("practice.db"))
    parser.add_argument("--at", help="ISO timestamp for reproducible cases")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Create fictional customers, products and coupons")
    catalog = commands.add_parser("catalog", help="List product prices and stock")
    catalog.add_argument("--category")
    catalog.add_argument("--include-inactive", action="store_true")
    for name in ("quote", "checkout"):
        command = commands.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
    order = commands.add_parser("order", help="View one order")
    order.add_argument("order_id")
    orders = commands.add_parser("orders", help="List recent orders")
    orders.add_argument("--customer")
    orders.add_argument("--status", choices=sorted(VALID_STATUSES))
    orders.add_argument("--limit", type=int, default=50)
    cancel = commands.add_parser("cancel", help="Cancel an unshipped order")
    cancel.add_argument("order_id")
    cancel.add_argument("--reason", required=True)
    ship = commands.add_parser("ship", help="Record dispatch of an order")
    ship.add_argument("order_id")
    ship.add_argument("--carrier", required=True)
    ship.add_argument("--tracking", required=True)
    refund = commands.add_parser("refund", help="Record a refund against a shipped order")
    refund.add_argument("order_id")
    refund.add_argument("--amount", required=True, help="Dollars, for example 5.25")
    refund.add_argument("--request-id", required=True)
    refund.add_argument("--reason", required=True)
    history = commands.add_parser("history", help="Show order events")
    history.add_argument("order_id")
    restock = commands.add_parser("restock")
    restock.add_argument("sku")
    restock.add_argument("--quantity", type=int, required=True)
    restock.add_argument("--reason", required=True)
    stock = commands.add_parser("stock-history")
    stock.add_argument("sku")
    stock.add_argument("--limit", type=int, default=50)
    low = commands.add_parser("low-stock")
    low.add_argument("--threshold", type=int, default=10)
    price = commands.add_parser("set-price")
    price.add_argument("sku")
    price.add_argument("--amount", required=True)
    active = commands.add_parser("set-active")
    active.add_argument("sku")
    active.add_argument("--value", choices=("yes", "no"), required=True)
    for name in ("report", "top-products", "export"):
        command = commands.add_parser(name)
        command.add_argument("--start", required=True)
        command.add_argument("--end", required=True)
        if name == "top-products":
            command.add_argument("--limit", type=int, default=10)
        if name == "export":
            command.add_argument("--output", type=Path, required=True)
    customer = commands.add_parser("customer-summary")
    customer.add_argument("customer_id")
    commands.add_parser("inventory-value")
    commands.add_parser("health")
    bulk = commands.add_parser("import-orders")
    bulk.add_argument("--input", type=Path, required=True)
    invoice = commands.add_parser("invoice", help="Write a text invoice")
    invoice.add_argument("order_id")
    invoice.add_argument("--output", type=Path, required=True)
    return parser


def write_invoice(db: Database, order_id: str, path: Path) -> dict:
    order = OrderService(db).view(order_id)
    customer = db.customer(order["customer_id"])
    lines = [
        f"Order {order_id}",
        f"Customer: {customer.name}",
        f"Created: {order['created_at']}",
        f"Status: {order['status']}",
        "Currency: USD",
        "",
    ]
    for item in order["items"]:
        price = format_money(item["unit_price_cents"])
        total = format_money(item["line_total_cents"])
        lines.append(f"{item['sku']}  {item['quantity']} x {price} = {total}")
    lines.extend([
        "",
        f"Subtotal: {format_money(order['subtotal_cents'])}",
        f"Discount: {format_money(order['discount_cents'])}",
        f"Shipping: {format_money(order['shipping_cents'])}",
        f"Tax: {format_money(order['tax_cents'])}",
        f"Total: {format_money(order['total_cents'])}",
        f"Refunded: {format_money(order['refunded_cents'])}",
    ])
    if not path.parent.is_dir():
        raise ServiceError("Invoice directory does not exist")
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return {"order_id": order_id, "path": str(path)}


def dispatch(args: argparse.Namespace, db: Database, now: datetime) -> Any:
    catalog = CatalogService(db)
    orders = OrderService(db)
    reports = ReportingService(db)
    command = args.command
    if command == "seed":
        return seed_demo(db, now)
    if command == "catalog":
        return catalog.list_products(args.category, args.include_inactive)
    if command in {"quote", "checkout"}:
        request = CheckoutRequest.from_dict(read_json(args.request))
        if command == "quote":
            return PricingService(db).quote(request, now).to_dict()
        return orders.checkout(request, now)
    if command == "order":
        return orders.view(args.order_id)
    if command == "orders":
        return orders.list_orders(args.customer, args.status, args.limit)
    if command == "cancel":
        return orders.cancel(args.order_id, args.reason, now)
    if command == "ship":
        return orders.ship(args.order_id, args.carrier, args.tracking, now)
    if command == "refund":
        return orders.refund(args.order_id, money_to_cents(args.amount),
                             args.request_id, args.reason, now)
    if command == "history":
        return orders.history(args.order_id)
    if command == "restock":
        return catalog.restock(args.sku.upper(), args.quantity, args.reason, now)
    if command == "stock-history":
        return catalog.stock_history(args.sku.upper(), args.limit)
    if command == "low-stock":
        return catalog.low_stock(args.threshold)
    if command == "set-price":
        return catalog.update_price(args.sku.upper(), money_to_cents(args.amount), now)
    if command == "set-active":
        return catalog.set_active(args.sku.upper(), args.value == "yes", now)
    if command == "report":
        return reports.sales(args.start, args.end)
    if command == "top-products":
        return reports.top_products(args.start, args.end, args.limit)
    if command == "export":
        return reports.export_orders(args.output, args.start, args.end)
    if command == "customer-summary":
        return reports.customer_summary(args.customer_id)
    if command == "inventory-value":
        return reports.inventory_value()
    if command == "health":
        return reports.health()
    if command == "import-orders":
        return import_orders(db, args.input, now)
    if command == "invoice":
        return write_invoice(db, args.order_id, args.output)
    raise ServiceError(f"Unsupported command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    db = None
    try:
        now = parse_time(args.at) if args.at else utc_now()
        db = Database(args.db)
        db.initialize()
        result = dispatch(args, db, now)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ServiceError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        LOG.debug("Command failed", exc_info=True)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
