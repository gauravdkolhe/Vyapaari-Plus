
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationError
from app.modules.inventory.models import Branch, StockBatch
from app.modules.inventory.valuation import (
    InventoryValuationEngine,
    ValuationMethod,
)


def add_branch_and_batches(db_session):
    db_session.add(
        Branch(
            id="branch-1",
            name="Main Warehouse",
            code="WH-01",
            is_warehouse=True,
        )
    )
    db_session.flush()

    now = datetime.now(timezone.utc)

    old = StockBatch(
        id="batch-old",
        branch_id="branch-1",
        product_id="product-1",
        batch_number="OLD",
        quantity=5,
        purchase_price=10.0,
        selling_price=15.0,
        expiry_date=now + timedelta(days=30),
        created_at=now - timedelta(days=10),
    )

    new = StockBatch(
        id="batch-new",
        branch_id="branch-1",
        product_id="product-1",
        batch_number="NEW",
        quantity=8,
        purchase_price=12.0,
        selling_price=18.0,
        expiry_date=now + timedelta(days=60),
        created_at=now - timedelta(days=2),
    )

    expired = StockBatch(
        id="batch-expired",
        branch_id="branch-1",
        product_id="product-1",
        batch_number="EXPIRED",
        quantity=100,
        purchase_price=1.0,
        selling_price=2.0,
        expiry_date=now - timedelta(days=1),
        created_at=now - timedelta(days=20),
    )

    db_session.add_all([old, new, expired])
    db_session.commit()


def test_fifo_allocates_across_batches_and_calculates_cost(db_session):
    add_branch_and_batches(db_session)

    result = InventoryValuationEngine(db_session).allocate(
        "branch-1", "product-1", 7, "FIFO"
    )

    assert [
        (item.batch_number, item.quantity)
        for item in result.allocations
    ] == [("OLD", 5), ("NEW", 2)]

    assert result.allocated_quantity == 7
    assert result.total_cost == Decimal("74.0")


def test_lifo_uses_newest_batch_first(db_session):
    add_branch_and_batches(db_session)

    result = InventoryValuationEngine(db_session).allocate(
        "branch-1", "product-1", 7, ValuationMethod.LIFO
    )

    assert [
        (item.batch_number, item.quantity)
        for item in result.allocations
    ] == [("NEW", 7)]

    assert result.total_cost == Decimal("84.0")


def test_expired_batches_are_excluded(db_session):
    add_branch_and_batches(db_session)

    result = InventoryValuationEngine(db_session).allocate(
        "branch-1", "product-1", 13
    )

    assert result.allocated_quantity == 13
    assert all(
        item.batch_number != "EXPIRED"
        for item in result.allocations
    )


def test_insufficient_stock_does_not_change_quantities(db_session):
    add_branch_and_batches(db_session)

    before = {
        batch.id: batch.quantity
        for batch in db_session.query(StockBatch).all()
    }

    with pytest.raises(ValidationError, match="Insufficient eligible stock"):
        InventoryValuationEngine(db_session).allocate(
            "branch-1", "product-1", 14
        )

    after = {
        batch.id: batch.quantity
        for batch in db_session.query(StockBatch).all()
    }

    assert after == before


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_invalid_quantity_is_rejected(db_session, quantity):
    with pytest.raises(ValidationError, match="positive integer"):
        InventoryValuationEngine(db_session).allocate(
            "branch-1", "product-1", quantity
        )


def test_invalid_method_is_rejected(db_session):
    with pytest.raises(ValidationError, match="FIFO or LIFO"):
        InventoryValuationEngine(db_session).allocate(
            "branch-1", "product-1", 1, "RANDOM"
        )
