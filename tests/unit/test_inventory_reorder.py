
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.modules.inventory.models  # Register inventory tables
from app.modules.inventory.models import Branch, StockBatch, PurchaseRequisition
from app.modules.inventory.reorder_service import InventoryReorderService


@pytest.fixture
def branch(db_session):
    branch = Branch(
        id=str(uuid4()),
        name="Test Warehouse",
        code=f"WH-{uuid4().hex[:8]}",
        is_warehouse=True,
    )
    db_session.add(branch)
    db_session.commit()
    return branch


def add_batch(
    db_session,
    branch_id,
    product_id,
    quantity,
    expiry_date=None,
    purchase_price=10.0,
):
    batch = StockBatch(
        id=str(uuid4()),
        branch_id=branch_id,
        product_id=product_id,
        batch_number=f"BATCH-{uuid4().hex[:8]}",
        quantity=quantity,
        purchase_price=purchase_price,
        selling_price=15.0,
        expiry_date=expiry_date,
    )
    db_session.add(batch)
    db_session.commit()
    return batch


def test_set_and_update_reorder_policy(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    policy = service.set_reorder_policy(
        branch.id, product_id, minimum_quantity=10, target_quantity=30
    )

    assert policy.minimum_quantity == 10
    assert policy.target_quantity == 30

    updated = service.set_reorder_policy(
        branch.id, product_id, minimum_quantity=15, target_quantity=40
    )

    assert updated.id == policy.id
    assert updated.minimum_quantity == 15
    assert updated.target_quantity == 40


def test_reject_invalid_reorder_thresholds(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    with pytest.raises(ValueError):
        service.set_reorder_policy(
            branch.id, product_id, minimum_quantity=-1, target_quantity=10
        )

    with pytest.raises(ValueError):
        service.set_reorder_policy(
            branch.id, product_id, minimum_quantity=10, target_quantity=5
        )


def test_available_stock_excludes_expired_batches(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())
    now = datetime.now(timezone.utc)

    add_batch(
        db_session,
        branch.id,
        product_id,
        quantity=8,
        expiry_date=now + timedelta(days=10),
    )
    add_batch(
        db_session,
        branch.id,
        product_id,
        quantity=20,
        expiry_date=now - timedelta(days=1),
    )
    add_batch(
        db_session,
        branch.id,
        product_id,
        quantity=3,
        expiry_date=None,
    )

    assert service.get_available_stock(branch.id, product_id) == 11


def test_low_stock_alert_and_automatic_requisition(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    service.set_reorder_policy(
        branch.id, product_id, minimum_quantity=10, target_quantity=25
    )
    add_batch(db_session, branch.id, product_id, quantity=4)

    alerts = service.get_low_stock_alerts()

    matching = [
        alert for alert in alerts
        if alert["branch_id"] == branch.id
        and alert["product_id"] == product_id
    ]

    assert len(matching) == 1
    assert matching[0]["available_quantity"] == 4
    assert matching[0]["reorder_quantity"] == 21

    requisitions = service.generate_low_stock_requisitions()
    matching_requisitions = [
        req for req in requisitions
        if req.branch_id == branch.id and req.product_id == product_id
    ]

    assert len(matching_requisitions) == 1
    assert matching_requisitions[0].requested_quantity == 21
    assert matching_requisitions[0].status == "OPEN"


def test_duplicate_open_requisition_is_prevented(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    first = service.create_purchase_requisition(
        branch.id, product_id, requested_quantity=10
    )
    second = service.create_purchase_requisition(
        branch.id, product_id, requested_quantity=20
    )

    assert first.id == second.id
    assert second.requested_quantity == 10

    open_requests = (
        db_session.query(PurchaseRequisition)
        .filter_by(
            branch_id=branch.id,
            product_id=product_id,
            status="OPEN",
        )
        .all()
    )
    assert len(open_requests) == 1


def test_closed_requisition_allows_new_open_request(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    first = service.create_purchase_requisition(
        branch.id, product_id, requested_quantity=10
    )

    service.update_requisition_status(first.id, "APPROVED")

    second = service.create_purchase_requisition(
        branch.id, product_id, requested_quantity=15
    )

    assert second.id != first.id
    assert second.status == "OPEN"
    assert second.requested_quantity == 15


def test_no_alert_when_stock_exceeds_minimum(db_session, branch):
    service = InventoryReorderService(db_session)
    product_id = str(uuid4())

    service.set_reorder_policy(
        branch.id, product_id, minimum_quantity=5, target_quantity=20
    )
    add_batch(db_session, branch.id, product_id, quantity=12)

    alerts = service.get_low_stock_alerts()

    assert not any(
        alert["branch_id"] == branch.id
        and alert["product_id"] == product_id
        for alert in alerts
    )
