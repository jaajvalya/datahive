#!/usr/bin/env python3
"""Generate synthetic pizza retail shop datasets (1000 rows each) into this directory."""
from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
N = 1000
RNG = random.Random(42)

FIRST = [
    "Ava", "Liam", "Olivia", "Noah", "Emma", "Oliver", "Sophia", "Elijah", "Isabella", "Lucas",
    "Mia", "Mason", "Amelia", "Ethan", "Harper", "James", "Evelyn", "Benjamin", "Abigail", "Henry",
    "Emily", "Alexander", "Ella", "Michael", "Scarlett", "Daniel", "Grace", "Jacob", "Chloe", "Logan",
    "Zoey", "Jackson", "Lily", "Sebastian", "Aria", "Jack", "Riley", "Aiden", "Nora", "Owen",
    "Hazel", "Samuel", "Aurora", "Matthew", "Penelope", "Joseph", "Layla", "Levi", "Nova", "David",
]
LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
]
CITIES = [
    ("Austin", "TX", "78701"), ("Dallas", "TX", "75201"), ("Houston", "TX", "77002"),
    ("San Antonio", "TX", "78205"), ("Denver", "CO", "80202"), ("Phoenix", "AZ", "85004"),
    ("Seattle", "WA", "98101"), ("Portland", "OR", "97201"), ("Chicago", "IL", "60601"),
    ("Atlanta", "GA", "30303"), ("Miami", "FL", "33101"), ("Tampa", "FL", "33602"),
    ("Nashville", "TN", "37203"), ("Charlotte", "NC", "28202"), ("Columbus", "OH", "43215"),
]
STREETS = [
    "Main St", "Oak Ave", "Pine Rd", "Maple Dr", "Cedar Ln", "Elm St", "Market St",
    "Lakeview Blvd", "River Rd", "Park Ave", "Sunset Blvd", "Highland Ave", "2nd St", "5th Ave",
]
PIZZA_BASES = [
    "Margherita", "Pepperoni", "BBQ Chicken", "Hawaiian", "Veggie Supreme", "Meat Lovers",
    "Four Cheese", "Buffalo Chicken", "Mushroom Truffle", "Spinach Feta", "Sicilian", "Neapolitan",
    "White Garlic", "Pesto Chicken", "Sausage & Peppers", "Supreme", "Ranch Special", "Spicy Diablo",
]
SIZES = ["Personal", "Medium", "Large", "Family"]
CRUSTS = ["Thin", "Hand-Tossed", "Pan", "Cauliflower", "Stuffed"]
SIDES = [
    "Garlic Knots", "Cheese Breadsticks", "Caesar Salad", "Garden Salad", "Chicken Wings",
    "Mozzarella Sticks", "Cinnamon Twists", "Chocolate Brownie", "Soft Drink", "Bottled Water",
]
MEMBERSHIP_TIERS = [
    ("BRONZE", 0, 0.00, 1.00),
    ("SILVER", 50, 0.05, 1.25),
    ("GOLD", 150, 0.10, 1.50),
    ("PLATINUM", 400, 0.15, 2.00),
]
ORDER_STATUSES = ["placed", "preparing", "baking", "out_for_delivery", "ready_pickup", "completed", "cancelled"]
ORDER_CHANNELS = ["in_store", "phone", "web", "mobile_app", "delivery_partner"]
PAYMENT_METHODS = ["cash", "visa", "mastercard", "amex", "apple_pay", "google_pay", "gift_card"]
ACCOUNT_TYPES = ["receivable", "payable", "cash", "card_clearing", "loyalty_liability", "inventory", "revenue", "cogs"]
EMP_ROLES = ["cashier", "pizza_chef", "shift_lead", "delivery_driver", "store_manager", "inventory_clerk"]


def write_csv(name: str, fieldnames: list[str], rows: list[dict]) -> None:
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path.name}: {len(rows)} rows")


def rand_dt(start: datetime, end: datetime) -> datetime:
    delta = int((end - start).total_seconds())
    return start + timedelta(seconds=RNG.randint(0, max(delta, 1)))


def phone() -> str:
    return f"({RNG.randint(200,999)}) {RNG.randint(200,999)}-{RNG.randint(1000,9999)}"


def email(first: str, last: str, i: int) -> str:
    domains = ["gmail.com", "yahoo.com", "outlook.com", "icloud.com", "example.com"]
    return f"{first}.{last}{i}@{RNG.choice(domains)}".lower()


def money(lo: float, hi: float) -> str:
    return f"{RNG.uniform(lo, hi):.2f}"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    start = datetime(2024, 1, 1, 8, 0, 0)
    end = datetime(2026, 7, 31, 22, 0, 0)

    # ---- stores (1000 synthetic locations / kiosks / franchise points) ----
    stores = []
    for i in range(1, N + 1):
        city, state, zipc = RNG.choice(CITIES)
        stores.append(
            {
                "store_id": f"STR{i:04d}",
                "store_name": f"Slice & Dice #{i:04d}",
                "store_type": RNG.choice(["dine_in", "takeout", "delivery_hub", "express_kiosk"]),
                "address_line": f"{RNG.randint(100, 9999)} {RNG.choice(STREETS)}",
                "city": city,
                "state": state,
                "zip": zipc,
                "phone": phone(),
                "open_time": "10:00",
                "close_time": "23:00",
                "is_active": "true" if RNG.random() > 0.05 else "false",
                "opened_on": (date(2015, 1, 1) + timedelta(days=RNG.randint(0, 4000))).isoformat(),
            }
        )
    write_csv(
        "stores.csv",
        [
            "store_id", "store_name", "store_type", "address_line", "city", "state", "zip",
            "phone", "open_time", "close_time", "is_active", "opened_on",
        ],
        stores,
    )

    # ---- suppliers ----
    suppliers = []
    supplier_names = [
        "Valley Farms Dairy", "Roma Tomato Co", "Harvest Flour Mills", "Coastal Seafoods",
        "GreenLeaf Produce", "FireOven Packaging", "Urban Beverage Dist", "Artisan Cheese Works",
    ]
    for i in range(1, N + 1):
        city, state, zipc = RNG.choice(CITIES)
        suppliers.append(
            {
                "supplier_id": f"SUP{i:04d}",
                "supplier_name": f"{RNG.choice(supplier_names)} {i}",
                "category": RNG.choice(
                    ["dairy", "produce", "meat", "dry_goods", "packaging", "beverages", "equipment"]
                ),
                "contact_name": f"{RNG.choice(FIRST)} {RNG.choice(LAST)}",
                "email": f"orders{i}@supplier{i % 97}.example.com",
                "phone": phone(),
                "city": city,
                "state": state,
                "zip": zipc,
                "payment_terms_days": str(RNG.choice([15, 30, 45, 60])),
                "is_preferred": "true" if RNG.random() > 0.7 else "false",
            }
        )
    write_csv(
        "suppliers.csv",
        [
            "supplier_id", "supplier_name", "category", "contact_name", "email", "phone",
            "city", "state", "zip", "payment_terms_days", "is_preferred",
        ],
        suppliers,
    )

    # ---- employees ----
    employees = []
    for i in range(1, N + 1):
        first, last = RNG.choice(FIRST), RNG.choice(LAST)
        hire = date(2018, 1, 1) + timedelta(days=RNG.randint(0, 2800))
        employees.append(
            {
                "employee_id": f"EMP{i:04d}",
                "store_id": stores[RNG.randrange(N)]["store_id"],
                "first_name": first,
                "last_name": last,
                "email": email(first, last, i),
                "phone": phone(),
                "role": RNG.choice(EMP_ROLES),
                "hire_date": hire.isoformat(),
                "hourly_rate": money(12, 32),
                "employment_status": RNG.choice(["active", "active", "active", "on_leave", "terminated"]),
            }
        )
    write_csv(
        "employees.csv",
        [
            "employee_id", "store_id", "first_name", "last_name", "email", "phone",
            "role", "hire_date", "hourly_rate", "employment_status",
        ],
        employees,
    )

    # ---- products (menu SKUs) ----
    products = []
    for i in range(1, N + 1):
        if i <= 700:
            base = RNG.choice(PIZZA_BASES)
            size = RNG.choice(SIZES)
            crust = RNG.choice(CRUSTS)
            name = f"{base} ({size}, {crust})"
            category = "pizza"
            unit_cost = RNG.uniform(2.5, 9.5)
            unit_price = unit_cost * RNG.uniform(2.2, 3.4)
        else:
            name = f"{RNG.choice(SIDES)} #{i}"
            category = RNG.choice(["side", "dessert", "beverage", "addon"])
            unit_cost = RNG.uniform(0.4, 4.0)
            unit_price = unit_cost * RNG.uniform(2.0, 3.8)
        products.append(
            {
                "product_id": f"PRD{i:04d}",
                "product_name": name,
                "category": category,
                "size": size if category == "pizza" else "",
                "crust": crust if category == "pizza" else "",
                "is_vegetarian": "true" if "Veggie" in name or "Margherita" in name or category != "pizza" and RNG.random() > 0.5 else "false",
                "unit_cost": f"{unit_cost:.2f}",
                "unit_price": f"{unit_price:.2f}",
                "calories": str(RNG.randint(120, 1400)),
                "is_active": "true" if RNG.random() > 0.08 else "false",
                "supplier_id": suppliers[RNG.randrange(N)]["supplier_id"],
            }
        )
    write_csv(
        "products.csv",
        [
            "product_id", "product_name", "category", "size", "crust", "is_vegetarian",
            "unit_cost", "unit_price", "calories", "is_active", "supplier_id",
        ],
        products,
    )

    # ---- customers ----
    customers = []
    for i in range(1, N + 1):
        first, last = RNG.choice(FIRST), RNG.choice(LAST)
        city, state, zipc = RNG.choice(CITIES)
        created = rand_dt(start, end)
        customers.append(
            {
                "customer_id": f"CUS{i:04d}",
                "first_name": first,
                "last_name": last,
                "email": email(first, last, i),
                "phone": phone(),
                "address_line": f"{RNG.randint(10, 9999)} {RNG.choice(STREETS)}",
                "city": city,
                "state": state,
                "zip": zipc,
                "preferred_store_id": stores[RNG.randrange(N)]["store_id"],
                "preferred_channel": RNG.choice(ORDER_CHANNELS),
                "marketing_opt_in": "true" if RNG.random() > 0.35 else "false",
                "created_at": created.isoformat(sep=" "),
            }
        )
    write_csv(
        "customers.csv",
        [
            "customer_id", "first_name", "last_name", "email", "phone", "address_line",
            "city", "state", "zip", "preferred_store_id", "preferred_channel",
            "marketing_opt_in", "created_at",
        ],
        customers,
    )

    # ---- memberships (1:1 with customers) ----
    memberships = []
    for i in range(1, N + 1):
        tier_code, min_pts, discount, points_mult = RNG.choice(MEMBERSHIP_TIERS)
        joined = rand_dt(start, end)
        points = RNG.randint(min_pts, min_pts + 800)
        memberships.append(
            {
                "membership_id": f"MEM{i:04d}",
                "customer_id": customers[i - 1]["customer_id"],
                "tier_code": tier_code,
                "tier_name": tier_code.title() + " Slice Club",
                "points_balance": str(points),
                "lifetime_points": str(points + RNG.randint(0, 2000)),
                "discount_pct": f"{discount:.2f}",
                "points_multiplier": f"{points_mult:.2f}",
                "status": RNG.choice(["active", "active", "active", "paused", "expired"]),
                "joined_at": joined.date().isoformat(),
                "renewal_date": (joined.date() + timedelta(days=365)).isoformat(),
                "referral_code": f"PIZZA{i:04d}",
            }
        )
    write_csv(
        "memberships.csv",
        [
            "membership_id", "customer_id", "tier_code", "tier_name", "points_balance",
            "lifetime_points", "discount_pct", "points_multiplier", "status",
            "joined_at", "renewal_date", "referral_code",
        ],
        memberships,
    )

    # ---- stock (inventory snapshot per product/store-ish) ----
    stock = []
    for i in range(1, N + 1):
        product = products[i - 1]
        store = stores[RNG.randrange(N)]
        on_hand = RNG.randint(0, 250)
        reorder = RNG.randint(10, 60)
        stock.append(
            {
                "stock_id": f"STK{i:04d}",
                "store_id": store["store_id"],
                "product_id": product["product_id"],
                "quantity_on_hand": str(on_hand),
                "quantity_reserved": str(RNG.randint(0, min(20, on_hand))),
                "reorder_level": str(reorder),
                "reorder_quantity": str(RNG.randint(reorder, reorder * 3)),
                "unit_cost": product["unit_cost"],
                "bin_location": f"{RNG.choice(['A','B','C','F'])}-{RNG.randint(1,20):02d}",
                "last_counted_at": rand_dt(start, end).isoformat(sep=" "),
                "stock_status": "out_of_stock"
                if on_hand == 0
                else ("low" if on_hand <= reorder else "ok"),
            }
        )
    write_csv(
        "stock.csv",
        [
            "stock_id", "store_id", "product_id", "quantity_on_hand", "quantity_reserved",
            "reorder_level", "reorder_quantity", "unit_cost", "bin_location",
            "last_counted_at", "stock_status",
        ],
        stock,
    )

    # ---- orders ----
    orders = []
    for i in range(1, N + 1):
        cust = customers[RNG.randrange(N)]
        store = stores[RNG.randrange(N)]
        placed = rand_dt(start, end)
        status = RNG.choice(ORDER_STATUSES)
        channel = RNG.choice(ORDER_CHANNELS)
        subtotal = RNG.uniform(8, 95)
        tax = subtotal * 0.0825
        delivery_fee = 3.99 if channel in {"delivery_partner", "mobile_app", "web"} and RNG.random() > 0.4 else 0.0
        discount = subtotal * RNG.choice([0, 0, 0, 0.05, 0.10, 0.15])
        total = max(subtotal + tax + delivery_fee - discount, 0)
        orders.append(
            {
                "order_id": f"ORD{i:04d}",
                "customer_id": cust["customer_id"],
                "store_id": store["store_id"],
                "employee_id": employees[RNG.randrange(N)]["employee_id"],
                "membership_id": memberships[int(cust["customer_id"][3:]) - 1]["membership_id"],
                "order_channel": channel,
                "order_type": RNG.choice(["dine_in", "takeout", "delivery"]),
                "order_status": status,
                "ordered_at": placed.isoformat(sep=" "),
                "promised_at": (placed + timedelta(minutes=RNG.randint(20, 75))).isoformat(sep=" "),
                "subtotal_amount": f"{subtotal:.2f}",
                "tax_amount": f"{tax:.2f}",
                "delivery_fee": f"{delivery_fee:.2f}",
                "discount_amount": f"{discount:.2f}",
                "total_amount": f"{total:.2f}",
                "currency": "USD",
            }
        )
    write_csv(
        "orders.csv",
        [
            "order_id", "customer_id", "store_id", "employee_id", "membership_id",
            "order_channel", "order_type", "order_status", "ordered_at", "promised_at",
            "subtotal_amount", "tax_amount", "delivery_fee", "discount_amount",
            "total_amount", "currency",
        ],
        orders,
    )

    # ---- order_items (1000 rows; ~1 item mapping across orders with reuse) ----
    order_items = []
    for i in range(1, N + 1):
        order = orders[i - 1]
        product = products[RNG.randrange(N)]
        qty = RNG.randint(1, 4)
        unit_price = float(product["unit_price"])
        line_total = qty * unit_price
        order_items.append(
            {
                "order_item_id": f"ORI{i:04d}",
                "order_id": order["order_id"],
                "product_id": product["product_id"],
                "product_name": product["product_name"],
                "quantity": str(qty),
                "unit_price": f"{unit_price:.2f}",
                "line_discount": money(0, min(3, unit_price)),
                "line_total": f"{line_total:.2f}",
                "special_instructions": RNG.choice(
                    ["", "", "", "extra cheese", "light sauce", "well done", "no onions", "cut in squares"]
                ),
            }
        )
    write_csv(
        "order_items.csv",
        [
            "order_item_id", "order_id", "product_id", "product_name", "quantity",
            "unit_price", "line_discount", "line_total", "special_instructions",
        ],
        order_items,
    )

    # ---- sales (transactional sale headers aligned to completed-ish orders) ----
    sales = []
    for i in range(1, N + 1):
        order = orders[i - 1]
        sales.append(
            {
                "sale_id": f"SAL{i:04d}",
                "order_id": order["order_id"],
                "store_id": order["store_id"],
                "customer_id": order["customer_id"],
                "sale_ts": order["ordered_at"],
                "sale_channel": order["order_channel"],
                "gross_amount": order["subtotal_amount"],
                "discount_amount": order["discount_amount"],
                "tax_amount": order["tax_amount"],
                "net_amount": order["total_amount"],
                "items_count": order_items[i - 1]["quantity"],
                "is_member_sale": "true",
                "business_date": order["ordered_at"][:10],
            }
        )
    write_csv(
        "sales.csv",
        [
            "sale_id", "order_id", "store_id", "customer_id", "sale_ts", "sale_channel",
            "gross_amount", "discount_amount", "tax_amount", "net_amount", "items_count",
            "is_member_sale", "business_date",
        ],
        sales,
    )

    # ---- accounts / payments ledger ----
    accounts = []
    for i in range(1, N + 1):
        order = orders[i - 1]
        method = RNG.choice(PAYMENT_METHODS)
        status = "voided" if order["order_status"] == "cancelled" else RNG.choice(
            ["captured", "captured", "captured", "pending", "refunded"]
        )
        amount = float(order["total_amount"])
        if status == "refunded":
            amount = -abs(amount)
        accounts.append(
            {
                "account_txn_id": f"ACC{i:04d}",
                "order_id": order["order_id"],
                "customer_id": order["customer_id"],
                "store_id": order["store_id"],
                "account_type": RNG.choice(ACCOUNT_TYPES),
                "payment_method": method,
                "txn_status": status,
                "amount": f"{amount:.2f}",
                "currency": "USD",
                "auth_code": f"{RNG.randint(100000, 999999)}",
                "txn_ts": order["ordered_at"],
                "settlement_date": order["ordered_at"][:10],
                "reference_no": f"REF{i:08d}",
            }
        )
    write_csv(
        "accounts.csv",
        [
            "account_txn_id", "order_id", "customer_id", "store_id", "account_type",
            "payment_method", "txn_status", "amount", "currency", "auth_code",
            "txn_ts", "settlement_date", "reference_no",
        ],
        accounts,
    )

    # ---- stock_movements (related inventory events) ----
    movements = []
    for i in range(1, N + 1):
        product = products[RNG.randrange(N)]
        store = stores[RNG.randrange(N)]
        mtype = RNG.choice(["receipt", "sale", "waste", "transfer_in", "transfer_out", "adjustment"])
        qty = RNG.randint(1, 40)
        if mtype in {"sale", "waste", "transfer_out"}:
            qty = -qty
        movements.append(
            {
                "movement_id": f"MOV{i:04d}",
                "store_id": store["store_id"],
                "product_id": product["product_id"],
                "supplier_id": product["supplier_id"] if mtype == "receipt" else "",
                "movement_type": mtype,
                "quantity": str(qty),
                "unit_cost": product["unit_cost"],
                "reference_id": orders[RNG.randrange(N)]["order_id"] if mtype == "sale" else f"DOC{i:04d}",
                "moved_at": rand_dt(start, end).isoformat(sep=" "),
                "notes": RNG.choice(["", "", "weekly delivery", "end of day count", "damaged goods", "promo prep"]),
            }
        )
    write_csv(
        "stock_movements.csv",
        [
            "movement_id", "store_id", "product_id", "supplier_id", "movement_type",
            "quantity", "unit_cost", "reference_id", "moved_at", "notes",
        ],
        movements,
    )

    # ---- promotions ----
    promotions = []
    for i in range(1, N + 1):
        start_d = date(2024, 1, 1) + timedelta(days=RNG.randint(0, 800))
        promotions.append(
            {
                "promotion_id": f"PRO{i:04d}",
                "promo_code": f"SLICE{i:04d}",
                "promo_name": RNG.choice(
                    [
                        "Two for Tuesday",
                        "Lunch Special",
                        "Family Feast",
                        "App Exclusive",
                        "Member Double Points",
                        "Free Delivery Weekend",
                        "Student Night",
                    ]
                )
                + f" {i}",
                "discount_type": RNG.choice(["percent", "fixed", "bogo", "free_delivery"]),
                "discount_value": money(1, 25),
                "min_order_amount": money(10, 40),
                "start_date": start_d.isoformat(),
                "end_date": (start_d + timedelta(days=RNG.randint(7, 90))).isoformat(),
                "channel": RNG.choice(ORDER_CHANNELS + ["all"]),
                "is_active": "true" if RNG.random() > 0.3 else "false",
            }
        )
    write_csv(
        "promotions.csv",
        [
            "promotion_id", "promo_code", "promo_name", "discount_type", "discount_value",
            "min_order_amount", "start_date", "end_date", "channel", "is_active",
        ],
        promotions,
    )

    # ---- deliveries ----
    deliveries = []
    for i in range(1, N + 1):
        order = orders[i - 1]
        cust = next(c for c in customers if c["customer_id"] == order["customer_id"])
        deliveries.append(
            {
                "delivery_id": f"DEL{i:04d}",
                "order_id": order["order_id"],
                "driver_employee_id": employees[RNG.randrange(N)]["employee_id"],
                "delivery_address": cust["address_line"],
                "city": cust["city"],
                "state": cust["state"],
                "zip": cust["zip"],
                "status": RNG.choice(
                    ["queued", "assigned", "en_route", "delivered", "failed", "returned"]
                ),
                "dispatched_at": order["ordered_at"],
                "delivered_at": (
                    datetime.fromisoformat(order["ordered_at"]) + timedelta(minutes=RNG.randint(15, 90))
                ).isoformat(sep=" "),
                "distance_miles": f"{RNG.uniform(0.5, 12):.1f}",
                "tip_amount": money(0, 12),
            }
        )
    write_csv(
        "deliveries.csv",
        [
            "delivery_id", "order_id", "driver_employee_id", "delivery_address", "city",
            "state", "zip", "status", "dispatched_at", "delivered_at", "distance_miles",
            "tip_amount",
        ],
        deliveries,
    )

    print(f"\nDone. Files written under {OUT}")


if __name__ == "__main__":
    main()
