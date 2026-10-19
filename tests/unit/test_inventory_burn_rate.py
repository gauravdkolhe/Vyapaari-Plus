
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.modules.inventory.models
from app.modules.inventory.models import Branch, StockBatch, StockMovement
from app.modules.inventory.burn_rate import InventoryBurnRateService


@pytest.fixture
def branch(db_session):
    item = Branch(
        id=str(uuid4()),
        name="Burn Rate Warehouse",
        code=f"BR-{uuid4().hex[:8]}",
        is_warehouse=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def add_batch(db_session, branch_id, product_id, quantity):
    batch = StockBatch(
        id=str(uuid4()),
        branch_id=branch_id,
        product_id=product_id,
        batch_number=f"B-{uuid4().hex[:8]}",
        quantity=quantity,
        purchase_price=10.0,
        selling_price=15.0,
    )
    db_session.add(batch)
    db_session.commit()
    return batch


def add_movement(
    db_session,
    branch_id,
    product_id,
    movement_type,
    quantity_delta,
    created_at,
):
    batch = (
        db_session.query(StockBatch)
        .filter_by(branch_id=branch_id, product_id=product_id)
        .first()
    )

    movement = StockMovement(
        id=str(uuid4()),
        branch_id=branch_id,
        batch_id=batch.id,
        product_id=product_id,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        unit_cost=10.0,
        created_at=created_at,
    )
    db_session.add(movement)
    db_session.commit()
    return movement


def test_calculates_daily_burn_rate(db_session, branch):
    product_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    add_batch(db_session, branch.id, product_id, quantity=12)

    add_movement(
        db_session,
        branch.id,
        product_id,
        "SALE",
        -40,
        now - timedelta(days=10),
    )
    add_movement(
        db_session,
        branch.id,
        product_id,
        "SALE",
        -20,
        now - timedelta(days=5),
    )

    result = InventoryBurnRateService(db_session).calculate(
        branch.id, product_id, lookback_days=30, as_of=now
    )

    assert result["total_consumed"] == 60
    assert result["average_daily_burn_rate"] == 2.0
    assert result["available_stock"] == 12
    assert result["days_until_stockout"] == 6.0
    assert result["days_until_stockout_rounded"] == 6


def test_ignores_non_sale_movements(db_session, branch):
    product_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    add_batch(db_session, branch.id, product_id, quantity=50)

    add_movement(
        db_session,
        branch.id,
        product_id,
        "TRANSFER",
        -20,
        now - timedelta(days=2),
    )
    add_movement(
        db_session,
        branch.id,
        product_id,
        "SHRINKAGE",
        -5,
        now - timedelta(days=1),
    )

    result = InventoryBurnRateService(db_session).calculate(
        branch.id, product_id, lookback_days=30, as_of=now
    )

    assert result["total_consumed"] == 0
    assert result["average_daily_burn_rate"] == 0
    assert result["days_until_stockout"] is None
    assert result["estimated_stockout_date"] is None


def test_ignores_sales_outside_lookback_period(db_session, branch):
    product_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    add_batch(db_session, branch.id, product_id, quantity=20)

    add_movement(
        db_session,
        branch.id,
        product_id,
        "SALE",
        -100,
        now - timedelta(days=45),
    )
    add_movement(
        db_session,
        branch.id,
        product_id,
        "SALE",
        -10,
        now - timedelta(days=5),
    )

    result = InventoryBurnRateService(db_session).calculate(
        branch.id, product_id, lookback_days=30, as_of=now
    )

    assert result["total_consumed"] == 10
    assert result["average_daily_burn_rate"] == 0.33


def test_rejects_invalid_lookback_period(db_session, branch):
    with pytest.raises(ValueError):
        InventoryBurnRateService(db_session).calculate(
            branch.id, str(uuid4()), lookback_days=0
        )


def test_low_runway_warning(db_session, branch):
    product_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    add_batch(db_session, branch.id, product_id, quantity=4)

    add_movement(
        db_session,
        branch.id,
        product_id,
        "SALE",
        -30,
        now - timedelta(days=10),
    )

    warnings = InventoryBurnRateService(db_session).get_low_runway_products(
        branch.id,
        [product_id],
        warning_days=7,
        lookback_days=30,
    )

    assert len(warnings) == 1
    assert warnings[0]["product_id"] == product_id
    assert warnings[0]["days_until_stockout"] == 4.0
