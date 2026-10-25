
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import streamlit as st

# Configure a separate database BEFORE importing application modules.
ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = Path(__file__).resolve().parent
DEMO_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = f"sqlite:///{DEMO_DIR / 'vyapaari_demo.db'}"
os.environ["APP_DEBUG"] = "false"

from app.db.base import Base
from app.db.session import engine, SessionLocal
from app.modules.inventory import models
from app.modules.inventory.models import Branch, StockBatch, ReorderPolicy
from app.modules.inventory.valuation import (
    InventoryValuationEngine,
    ValuationMethod,
)
from app.modules.inventory.reorder_service import InventoryReorderService


st.set_page_config(
    page_title="Vyapaari+ Inventory Demo",
    page_icon="📦",
    layout="wide",
)

Base.metadata.create_all(bind=engine)


def seed_demo_data():
    """Seed fictional sample data once in the isolated demo database."""
    with SessionLocal() as db:
        if db.query(Branch).count() > 0:
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        branches = [
            Branch(
                id="warehouse-mumbai",
                name="Mumbai Central Warehouse",
                code="MUM-01",
                address="Mumbai, Maharashtra",
                is_warehouse=True,
            ),
            Branch(
                id="warehouse-pune",
                name="Pune Distribution Centre",
                code="PUN-01",
                address="Pune, Maharashtra",
                is_warehouse=True,
            ),
        ]
        db.add_all(branches)
        db.flush()

        products = [
            {
                "id": "product-laptop",
                "name": "Business Laptop",
                "batches": [
                    ("LAP-OLD", 12, 42000.0, 20),
                    ("LAP-MID", 18, 43500.0, 10),
                    ("LAP-NEW", 20, 45000.0, 2),
                ],
                "minimum": 15,
                "target": 40,
            },
            {
                "id": "product-mouse",
                "name": "Wireless Mouse",
                "batches": [
                    ("MOU-OLD", 5, 450.0, 15),
                    ("MOU-NEW", 7, 500.0, 3),
                ],
                "minimum": 20,
                "target": 50,
            },
            {
                "id": "product-keyboard",
                "name": "RGB Keyboard",
                "batches": [
                    ("KEY-OLD", 25, 1200.0, 12),
                    ("KEY-NEW", 15, 1350.0, 1),
                ],
                "minimum": 10,
                "target": 30,
            },
        ]

        for product in products:
            product_id = product["id"]

            for branch_id, multiplier in [
                ("warehouse-mumbai", 1),
                ("warehouse-pune", 2),
            ]:
                for batch_no, qty, cost, age_days in product["batches"]:
                    # Give the Pune warehouse different quantities.
                    adjusted_qty = max(1, qty // multiplier)
                    db.add(
                        StockBatch(
                            id=str(uuid4()),
                            branch_id=branch_id,
                            product_id=product_id,
                            batch_number=f"{branch_id[-3:]}-{batch_no}",
                            quantity=adjusted_qty,
                            purchase_price=cost,
                            selling_price=cost * 1.15,
                            created_at=now - timedelta(days=age_days),
                        )
                    )

                db.add(
                    ReorderPolicy(
                        id=str(uuid4()),
                        branch_id=branch_id,
                        product_id=product_id,
                        minimum_quantity=product["minimum"],
                        target_quantity=product["target"],
                    )
                )

        db.commit()


seed_demo_data()

PRODUCT_NAMES = {
    "product-laptop": "Business Laptop",
    "product-mouse": "Wireless Mouse",
    "product-keyboard": "RGB Keyboard",
}

st.title("📦 Vyapaari+ Inventory Engine")
st.caption(
    "Interactive demonstration · Multi-warehouse stock · FIFO/LIFO valuation · Reorder alerts"
)

st.info(
    "Demo environment: fictional sample data in a separate SQLite database. "
    "Valuation previews do not deduct stock."
)

with SessionLocal() as db:
    branches = db.query(Branch).order_by(Branch.name).all()
    branch_map = {branch.name: branch.id for branch in branches}

    all_batches = db.query(StockBatch).all()
    total_units = sum(max(0, batch.quantity) for batch in all_batches)
    stock_value = sum(
        max(0, batch.quantity) * batch.purchase_price
        for batch in all_batches
    )

    alerts = InventoryReorderService(db).get_low_stock_alerts()

    col1, col2, col3 = st.columns(3)
    col1.metric("Warehouses", len(branches))
    col2.metric("Units in stock", f"{total_units:,}")
    col3.metric("Low-stock alerts", len(alerts))

    st.metric("Inventory purchase value", f"₹{stock_value:,.2f}")

    st.divider()
    st.subheader("Warehouse inventory")

    selected_branch_name = st.selectbox(
        "Select warehouse",
        list(branch_map.keys()),
    )
    branch_id = branch_map[selected_branch_name]

    inventory_rows = []
    for product_id, product_name in PRODUCT_NAMES.items():
        batches = (
            db.query(StockBatch)
            .filter(
                StockBatch.branch_id == branch_id,
                StockBatch.product_id == product_id,
            )
            .order_by(StockBatch.created_at, StockBatch.id)
            .all()
        )

        inventory_rows.append(
            {
                "Product": product_name,
                "Product ID": product_id,
                "Batches": len(batches),
                "Units": sum(max(0, b.quantity) for b in batches),
                "Purchase value": sum(
                    max(0, b.quantity) * b.purchase_price for b in batches
                ),
            }
        )

    st.dataframe(
        inventory_rows,
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("FIFO / LIFO valuation preview")

    product_id = st.selectbox(
        "Product",
        list(PRODUCT_NAMES.keys()),
        format_func=lambda value: PRODUCT_NAMES[value],
        key="valuation_product",
    )

    method_name = st.radio(
        "Valuation method",
        ["FIFO", "LIFO"],
        horizontal=True,
    )

    available_batches = (
        db.query(StockBatch)
        .filter(
            StockBatch.branch_id == branch_id,
            StockBatch.product_id == product_id,
        )
        .all()
    )
    available_units = sum(max(0, b.quantity) for b in available_batches)

    quantity = st.number_input(
        "Quantity to value",
        min_value=1,
        max_value=max(1, available_units),
        value=1,
        step=1,
    )

    if st.button("Calculate valuation", type="primary"):
        try:
            result = InventoryValuationEngine(db).allocate(
                branch_id=branch_id,
                product_id=product_id,
                quantity=int(quantity),
                method=ValuationMethod(method_name),
            )

            st.success(f"{method_name} allocation calculated successfully.")
            left, right = st.columns(2)
            left.metric("Allocated units", result.allocated_quantity)
            right.metric("Total cost", f"₹{result.total_cost:,.2f}")

            allocation_rows = [
                {
                    "Batch": item.batch_number,
                    "Quantity": item.quantity,
                    "Unit cost": f"₹{item.unit_cost:,.2f}",
                    "Total cost": f"₹{item.total_cost:,.2f}",
                }
                for item in result.allocations
            ]
            st.dataframe(
                allocation_rows,
                use_container_width=True,
                hide_index=True,
            )
            st.caption("This is a valuation preview; stock quantities remain unchanged.")
        except Exception as exc:
            st.error(f"Unable to calculate allocation: {exc}")

    st.divider()
    st.subheader("Reorder alerts")

    if alerts:
        alert_rows = []
        for alert in alerts:
            alert_rows.append(
                {
                    "Warehouse": next(
                        (
                            b.name for b in branches
                            if b.id == alert["branch_id"]
                        ),
                        alert["branch_id"],
                    ),
                    "Product": PRODUCT_NAMES.get(
                        alert["product_id"], alert["product_id"]
                    ),
                    "Available": alert["available_quantity"],
                    "Minimum": alert["minimum_quantity"],
                    "Target": alert["target_quantity"],
                    "Suggested reorder": alert["reorder_quantity"],
                }
            )
        st.dataframe(
            alert_rows,
            width="stretch",
            hide_index=True,
        )
    else:
        st.success("No low-stock alerts at this time.")

st.caption("Vyapaari+ · Real-Time Multi-Warehouse Inventory & Stock Batch Engine")
