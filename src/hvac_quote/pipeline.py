"""Shared result type and formatting used by the generic quotation pipeline."""
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from .models import TakeoffResult
from .pricing import PricingResult
from .template import RecalcResult

@dataclass
class RunResult:
    pricing: PricingResult
    recalc: RecalcResult
    takeoff: TakeoffResult
    quote_json: Path
    excel_path: Path
    pdf_path: Path
