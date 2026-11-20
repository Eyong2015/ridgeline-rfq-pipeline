from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class RFQLine(BaseModel):
    line: int = Field(gt=0)
    as_written: str = Field(min_length=1)
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[str] = None
    ridgeline_part_number: Optional[str] = None
    match_method: Optional[
        Literal["ridgeline_part_number", "cross_reference", "description", "llm"]
    ] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = True
    flags: List[str] = Field(default_factory=list)
    note: Optional[str] = None
    product_reference: Optional[str] = Field(default=None, exclude=True)

    @field_validator("as_written")
    @classmethod
    def as_written_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("as_written must not be blank")
        return value


class RFQ(BaseModel):
    rfq_id: str = Field(min_length=1)
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    lines: List[RFQLine] = Field(default_factory=list)
    non_item_rows: List[str] = Field(default_factory=list)

    @field_validator("rfq_id")
    @classmethod
    def rfq_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rfq_id must not be blank")
        return value
