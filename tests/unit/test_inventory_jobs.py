
from uuid import uuid4

import app.modules.inventory.models
from app.modules.inventory.models import Branch, StockBatch
from app.modules.inventory.inventory_jobs import run_daily_inventory_check


def test_daily_job_reports_low_stock_and_creates_requisition(db_session):
    branch = Branch(
        id=str(uuid4()),
        name="Daily Job Warehouse",
        code=f"DJ-{uuid4().hex[:8]}",
        is_warehouse=True,
    )
    db_session.add(branch)
    db_session.commit()

    product_id = str(uuid4())

    batch = StockBatch(
        id=str(uuid4()),
        branch_id=branch.id,
        product_id=product_id,
        batch_number=f"B-{uuid4().hex[:8]}",
        quantity=2,
        purchase_price=10.0,
        selling_price=15.0,
    )
    db_session.add(batch)
    db_session.commit()

    from app.modules.inventory.reorder_service import InventoryReorderService

    InventoryReorderService(db_session).set_reorder_policy(
        branch.id,
        product_id,
        minimum_quantity=5,
        target_quantity=20,
    )

    result = run_daily_inventory_check(db=db_session)

    assert result["low_stock_alert_count"] == 1
    assert result["requisition_count"] == 1
    assert result["low_stock_alerts"][0]["product_id"] == product_id
    assert result["requisitions"][0]["requested_quantity"] == 18


def test_daily_job_does_not_duplicate_open_requisitions(db_session):
    branch = Branch(
        id=str(uuid4()),
        name="Repeat Job Warehouse",
        code=f"RJ-{uuid4().hex[:8]}",
        is_warehouse=True,
    )
    db_session.add(branch)
    db_session.commit()

    product_id = str(uuid4())

    db_session.add(
        StockBatch(
            id=str(uuid4()),
            branch_id=branch.id,
            product_id=product_id,
            batch_number=f"B-{uuid4().hex[:8]}",
            quantity=1,
            purchase_price=10.0,
            selling_price=15.0,
        )
    )
    db_session.commit()

    from app.modules.inventory.reorder_service import InventoryReorderService

    InventoryReorderService(db_session).set_reorder_policy(
        branch.id, product_id, minimum_quantity=5, target_quantity=20
    )

    first = run_daily_inventory_check(db=db_session)
    second = run_daily_inventory_check(db=db_session)

    assert first["requisition_count"] == 1
    assert second["requisition_count"] == 1

    from app.modules.inventory.models import PurchaseRequisition

    open_requests = (
        db_session.query(PurchaseRequisition)
        .filter_by(branch_id=branch.id, product_id=product_id, status="OPEN")
        .all()
    )
    assert len(open_requests) == 1
