"""Canonical invoice data and extraction metadata models."""
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

class TaxBreakdown(BaseModel):
    rate: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    taxable_amount: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    tax_amount: Optional[Decimal] = Field(default=None, allow_inf_nan=False)

class LineItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    unit_price: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    vat_rate: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    line_total: Optional[Decimal] = Field(default=None, allow_inf_nan=False)

class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_name: Optional[str] = None
    supplier_address: Optional[str] = None
    supplier_vat_number: Optional[str] = None
    supplier_kvk_number: Optional[str] = None
    supplier_iban: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    currency: Optional[str] = None
    purchase_order_reference: Optional[str] = None
    document_type: Optional[str] = Field(default=None, description="invoice or credit_note when stated")
    subtotal: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    vat_breakdown: list[TaxBreakdown] = Field(default_factory=list)
    total_vat: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    invoice_total: Optional[Decimal] = Field(default=None, allow_inf_nan=False)
    line_items: list[LineItem] = Field(default_factory=list)

class FieldEvidence(BaseModel):
    page: Optional[int] = None
    text: Optional[str] = None
    bounding_box: Optional[list[float]] = None
    manually_corrected: bool = False
    needs_review: bool = False
    validation_warnings: list[str] = Field(default_factory=list)
