
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    String,
    Integer,
    Float,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TransferStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    RECEIVED = "RECEIVED"
    RECONCILED = "RECONCILED"
    DISPUTED = "DISPUTED"


class Branch(Base, TimestampMixin):
    __tablename__ = "branches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    is_warehouse: Mapped[bool] = mapped_column(default=False, nullable=False)


class StockBatch(Base, TimestampMixin):
    __tablename__ = "stock_batches"

    __table_args__ = (
        Index("ix_stock_batches_branch_product", "branch_id", "product_id"),
        Index("ix_stock_batches_batch_number", "batch_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
        nullable=False,
    )
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("branches.id"),
        nullable=False,
    )
    batch_number: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    purchase_price: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )
    selling_price: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )
    expiry_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class StockTransfer(Base, TimestampMixin):
    """Model for inter-store and branch stock transfers."""

    __tablename__ = "stock_transfers"

    __table_args__ = (
        Index(
            "ix_stock_transfers_source_status",
            "source_branch_id",
            "status",
        ),
        Index(
            "ix_stock_transfers_destination_status",
            "destination_branch_id",
            "status",
        ),
        Index("ix_stock_transfers_dispatched_at", "dispatched_at"),
        Index(
            "ix_stock_transfers_status_created",
            "status",
            "created_at",
        ),
        Index("ix_stock_transfers_created_at", "created_at"),
        Index("ix_stock_transfers_received_at", "received_at"),
        CheckConstraint(
            "source_branch_id <> destination_branch_id",
            name="ck_stock_transfers_distinct_branches",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    transfer_number: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )

    source_branch_id: Mapped[str] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    destination_branch_id: Mapped[str] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[TransferStatus] = mapped_column(
        Enum(TransferStatus),
        default=TransferStatus.DRAFT,
        nullable=False,
        index=True,
    )

    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    dispatched_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    received_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    vehicle_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    driver_contact: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    delivery_challan_number: Mapped[str | None] = mapped_column(
        String(50),
        unique=True,
        nullable=True,
    )
    gate_pass_number: Mapped[str | None] = mapped_column(
        String(50),
        unique=True,
        nullable=True,
    )

    carrier_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["TransferItem"]] = relationship(
        "TransferItem",
        back_populates="transfer",
        cascade="all, delete-orphan",
    )

    reconciliation: Mapped["ReconciliationLog | None"] = relationship(
        "ReconciliationLog",
        back_populates="transfer",
        uselist=False,
        cascade="all, delete-orphan",
    )

    carrier_logs: Mapped[list["CarrierLog"]] = relationship(
        "CarrierLog",
        back_populates="transfer",
        cascade="all, delete-orphan",
        order_by="CarrierLog.logged_at",
    )


class TransferItem(Base, TimestampMixin):
    """Items included in a stock transfer."""

    __tablename__ = "transfer_items"

    __table_args__ = (
        Index("ix_transfer_items_transfer_id", "transfer_id"),
        Index(
            "ix_transfer_items_product_batch",
            "product_id",
            "batch_number",
        ),
        CheckConstraint(
            "quantity_dispatched >= 0",
            name="ck_transfer_items_dispatched_non_negative",
        ),
        CheckConstraint(
            "quantity_received >= 0",
            name="ck_transfer_items_received_non_negative",
        ),
        CheckConstraint(
            "quantity_damaged >= 0",
            name="ck_transfer_items_damaged_non_negative",
        ),
        CheckConstraint(
            "quantity_received + quantity_damaged <= quantity_dispatched",
            name="ck_transfer_items_received_damaged_within_dispatched",
        ),
        CheckConstraint(
            "unit_cost >= 0",
            name="ck_transfer_items_unit_cost_non_negative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    transfer_id: Mapped[str] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="CASCADE"),
        nullable=False,
    )

    product_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    product_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )
    batch_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    quantity_dispatched: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    quantity_received: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    quantity_damaged: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    unit_cost: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    transfer: Mapped["StockTransfer"] = relationship(
        "StockTransfer",
        back_populates="items",
    )


class ReconciliationLog(Base, TimestampMixin):
    """Reconciliation record for inter-branch inventory transfers."""

    __tablename__ = "reconciliation_logs"

    __table_args__ = (
        Index(
            "ix_reconciliation_logs_status_reconciled_at",
            "resolution_status",
            "reconciled_at",
        ),
        Index("ix_reconciliation_logs_reconciled_by", "reconciled_by"),
        CheckConstraint(
            "resolution_status IN ('PENDING', 'RESOLVED')",
            name="ck_reconciliation_logs_resolution_status",
        ),
        CheckConstraint(
            "total_dispatched_units >= 0 AND total_received_units >= 0",
            name="ck_reconciliation_logs_units_non_negative",
        ),
        CheckConstraint(
            "variance_units = total_dispatched_units - total_received_units",
            name="ck_reconciliation_logs_variance_formula",
        ),
        CheckConstraint(
            "total_transit_loss_value >= 0",
            name="ck_reconciliation_logs_loss_non_negative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    transfer_id: Mapped[str] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    total_dispatched_units: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    total_received_units: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    variance_units: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    total_transit_loss_value: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    reconciled_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolution_status: Mapped[str] = mapped_column(
        String(50),
        default="PENDING",
        nullable=False,
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    transfer: Mapped["StockTransfer"] = relationship(
        "StockTransfer",
        back_populates="reconciliation",
    )

    approvals: Mapped[list["ReconciliationApproval"]] = relationship(
        "ReconciliationApproval",
        back_populates="reconciliation",
        cascade="save-update, merge",
        passive_deletes="all",
        order_by="ReconciliationApproval.decided_at",
    )


class ReconciliationApproval(Base, TimestampMixin):
    """Append-only approval history for a reconciliation."""

    __tablename__ = "reconciliation_approvals"

    __table_args__ = (
        Index(
            "ix_reconciliation_approvals_transfer",
            "transfer_id",
            "decided_at",
        ),
        Index(
            "ix_reconciliation_approvals_reconciliation",
            "reconciliation_id",
        ),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_reconciliation_approvals_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    transfer_id: Mapped[str] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_logs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    approver: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    reconciliation: Mapped["ReconciliationLog"] = relationship(
        "ReconciliationLog",
        back_populates="approvals",
    )


class CarrierLog(Base, TimestampMixin):
    """Record carrier movement and delivery events."""

    __tablename__ = "carrier_logs"

    __table_args__ = (
        Index(
            "ix_carrier_logs_transfer_logged",
            "transfer_id",
            "logged_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    transfer_id: Mapped[str] = mapped_column(
        ForeignKey("stock_transfers.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    location: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    logged_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    transfer: Mapped["StockTransfer"] = relationship(
        "StockTransfer",
        back_populates="carrier_logs",
    )


class StockMovement(Base):
    """Audit record for inventory quantity changes."""

    __tablename__ = "stock_movements"

    __table_args__ = (
        CheckConstraint(
            "quantity_delta <> 0",
            name="ck_stock_movements_nonzero_delta",
        ),
        CheckConstraint(
            "unit_cost >= 0",
            name="ck_stock_movements_unit_cost_nonnegative",
        ),
        Index(
            "ix_stock_movements_branch_product_time",
            "branch_id",
            "product_id",
            "created_at",
        ),
        Index("ix_stock_movements_batch", "batch_id"),
        Index(
            "ix_stock_movements_reference",
            "reference_type",
            "reference_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("stock_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )

    product_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    movement_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # Positive means stock added; negative means stock removed.
    quantity_delta: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    unit_cost: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    reference_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    reference_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )


class StockConsumptionRecord(Base):
    """Persist a checkout result so retries cannot consume stock twice."""

    __tablename__ = "stock_consumption_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Unique at database level to protect against concurrent duplicate requests.
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        index=True,
    )

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)

    # Keep exact decimal values as text to avoid float rounding in the saved result.
    total_cost: Mapped[str] = mapped_column(String(50), nullable=False)
    allocations_json: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )
