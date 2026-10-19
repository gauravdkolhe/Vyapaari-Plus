
"""Reorder policies, low-stock alerts, and purchase requisitions."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.inventory.models import (
    Branch,
    StockBatch,
    ReorderPolicy,
    PurchaseRequisition,
)


class InventoryReorderService:
    """Manage warehouse-specific stock thresholds and replenishment."""

    VALID_STATUSES = {"OPEN", "APPROVED", "ORDERED", "CANCELLED"}

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _validate_ids(branch_id: str, product_id: str) -> None:
        if not branch_id or not branch_id.strip():
            raise ValueError("Branch ID is required.")
        if not product_id or not product_id.strip():
            raise ValueError("Product ID is required.")

    def set_reorder_policy(
        self,
        branch_id: str,
        product_id: str,
        minimum_quantity: int,
        target_quantity: int,
    ) -> ReorderPolicy:
        """Create or update a product's reorder policy at a branch."""

        self._validate_ids(branch_id, product_id)

        if minimum_quantity < 0:
            raise ValueError("Minimum quantity cannot be negative.")
        if target_quantity < minimum_quantity:
            raise ValueError(
                "Target quantity must be greater than or equal to minimum quantity."
            )

        if self.db.get(Branch, branch_id) is None:
            raise ValueError(f"Branch '{branch_id}' does not exist.")

        policy = (
            self.db.query(ReorderPolicy)
            .filter_by(branch_id=branch_id, product_id=product_id)
            .first()
        )

        if policy is None:
            policy = ReorderPolicy(
                id=str(uuid4()),
                branch_id=branch_id,
                product_id=product_id,
                minimum_quantity=minimum_quantity,
                target_quantity=target_quantity,
            )
            self.db.add(policy)
        else:
            policy.minimum_quantity = minimum_quantity
            policy.target_quantity = target_quantity

        try:
            self.db.commit()
            self.db.refresh(policy)
            return policy
        except Exception:
            self.db.rollback()
            raise

    def get_available_stock(
        self,
        branch_id: str,
        product_id: str,
        as_of: datetime | None = None,
    ) -> int:
        """Return positive stock in non-expired batches."""

        self._validate_ids(branch_id, product_id)

        if as_of is None:
            as_of = datetime.now(timezone.utc)

        # SQLite commonly stores timezone-aware DateTime values as naive.
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)

        total = (
            self.db.query(func.coalesce(func.sum(StockBatch.quantity), 0))
            .filter(
                StockBatch.branch_id == branch_id,
                StockBatch.product_id == product_id,
                StockBatch.quantity > 0,
                or_(
                    StockBatch.expiry_date.is_(None),
                    StockBatch.expiry_date >= as_of,
                ),
            )
            .scalar()
        )

        return int(total or 0)

    def get_low_stock_alerts(self) -> list[dict]:
        """List branch/product combinations at or below minimum stock."""

        policies = (
            self.db.query(ReorderPolicy)
            .order_by(ReorderPolicy.branch_id, ReorderPolicy.product_id)
            .all()
        )

        alerts = []

        for policy in policies:
            available = self.get_available_stock(
                policy.branch_id,
                policy.product_id,
            )

            if available <= policy.minimum_quantity:
                alerts.append(
                    {
                        "branch_id": policy.branch_id,
                        "product_id": policy.product_id,
                        "available_quantity": available,
                        "minimum_quantity": policy.minimum_quantity,
                        "target_quantity": policy.target_quantity,
                        "reorder_quantity": max(
                            0, policy.target_quantity - available
                        ),
                    }
                )

        return alerts

    def create_purchase_requisition(
        self,
        branch_id: str,
        product_id: str,
        requested_quantity: int,
    ) -> PurchaseRequisition:
        """Create one open requisition per branch/product combination."""

        self._validate_ids(branch_id, product_id)

        if requested_quantity <= 0:
            raise ValueError("Requested quantity must be greater than zero.")

        if self.db.get(Branch, branch_id) is None:
            raise ValueError(f"Branch '{branch_id}' does not exist.")

        open_key = f"{branch_id}:{product_id}"

        existing = (
            self.db.query(PurchaseRequisition)
            .filter(
                PurchaseRequisition.open_key == open_key,
                PurchaseRequisition.status == "OPEN",
            )
            .first()
        )

        if existing is not None:
            return existing

        requisition = PurchaseRequisition(
            id=str(uuid4()),
            branch_id=branch_id,
            product_id=product_id,
            requested_quantity=requested_quantity,
            status="OPEN",
            open_key=open_key,
        )

        self.db.add(requisition)

        try:
            self.db.commit()
            self.db.refresh(requisition)
            return requisition
        except IntegrityError:
            # Another request may have created the same open requisition.
            self.db.rollback()

            existing = (
                self.db.query(PurchaseRequisition)
                .filter(PurchaseRequisition.open_key == open_key)
                .first()
            )

            if existing is not None:
                return existing

            raise
        except Exception:
            self.db.rollback()
            raise

    def generate_low_stock_requisitions(self) -> list[PurchaseRequisition]:
        """Create requisitions for low-stock policies that have none open."""

        created_or_existing = []

        for alert in self.get_low_stock_alerts():
            quantity = alert["reorder_quantity"]

            if quantity <= 0:
                continue

            requisition = self.create_purchase_requisition(
                branch_id=alert["branch_id"],
                product_id=alert["product_id"],
                requested_quantity=quantity,
            )
            created_or_existing.append(requisition)

        return created_or_existing

    def update_requisition_status(
        self,
        requisition_id: str,
        new_status: str,
    ) -> PurchaseRequisition:
        """Update a requisition and release its open slot when it closes."""

        new_status = new_status.strip().upper()

        if new_status not in self.VALID_STATUSES:
            raise ValueError(
                f"Invalid status. Choose one of: {', '.join(sorted(self.VALID_STATUSES))}"
            )

        requisition = self.db.get(PurchaseRequisition, requisition_id)

        if requisition is None:
            raise ValueError(
                f"Purchase requisition '{requisition_id}' does not exist."
            )

        if new_status == "OPEN":
            open_key = f"{requisition.branch_id}:{requisition.product_id}"

            existing = (
                self.db.query(PurchaseRequisition)
                .filter(
                    PurchaseRequisition.open_key == open_key,
                    PurchaseRequisition.id != requisition_id,
                )
                .first()
            )

            if existing is not None:
                raise ValueError(
                    "Another open requisition already exists for this branch and product."
                )

            requisition.open_key = open_key
        else:
            requisition.open_key = None

        requisition.status = new_status

        try:
            self.db.commit()
            self.db.refresh(requisition)
            return requisition
        except Exception:
            self.db.rollback()
            raise
