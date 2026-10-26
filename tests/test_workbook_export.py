from decimal import Decimal

import pytest
from openpyxl import load_workbook
from hvac_quote.models import QuantityItem
from hvac_quote.pricing import price_items
from hvac_quote.template import export_workbook, recalc_workbook


@pytest.mark.parametrize('tax_basis,total', [('subtotal','298.92'),('installation_and_auxiliary','285.72')])
def test_independent_excel_formulas_match_selected_tax_basis(tmp_path,tax_basis,total):
    items=[QuantityItem('unit','equipment','DEMO','台',Decimal('2'),unit_prices=(Decimal('100'),Decimal('10'))), QuantityItem('pipe','pipe','DN25','米',Decimal('10'),unit_prices=(Decimal('3'),Decimal('1')))]
    pricing=price_items(items,{},tax_rate=Decimal('0.06'),category_coefficients={'equipment':Decimal('1.1'),'pipe':Decimal('1')},tax_basis=tax_basis)
    # 2*(100+10)*1.1 + 10*(3+1) = 282; bases 282 / (22+40).
    assert pricing.total == Decimal(total)
    path=export_workbook(tmp_path/'quote.xlsx',project_name='=1+1',pricing=pricing,items=items,labels={},tax_rate=Decimal('0.06'),scope={'project':'=1+1'})
    source=load_workbook(path,data_only=False)
    assert source.active['A1'].data_type == 's'
    assert source['报价范围']['B2'].data_type == 's'
    assert source.active['M10'].data_type == 'f'
    recalculated=recalc_workbook(path,tmp_path)
    assert recalculated.values['total'] == Decimal(total)
    assert recalculated.values['subtotal'] == Decimal('282')
    assert recalculated.texts['uppercase']
