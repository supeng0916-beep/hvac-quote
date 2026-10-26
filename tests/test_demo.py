from decimal import Decimal
import pytest
from hvac_quote.demo import create_demo


@pytest.mark.parametrize('alternate_layout', [False, True])
def test_synthetic_demo_completes_quote_and_preserves_inputs(tmp_path, alternate_layout):
    result = create_demo(tmp_path / 'demo', alternate_layout=alternate_layout)
    assert Decimal(result['total']) == Decimal('3122.00')
    assert (tmp_path / 'demo' / result['excel']).is_file()
    assert (tmp_path / 'demo' / result['pdf']).is_file()
    assert result['equipment_quantity'] == 2
    assert sorted(result['lengths']) == [6.0, 8.0, 8.0]
    assert result['inputs_unchanged'] is True
