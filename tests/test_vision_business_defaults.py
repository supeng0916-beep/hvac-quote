from hvac_quote.new_quote_store import vision_adoption_business_data


def test_absent_tax_matches_rendered_zero_default():
    assert vision_adoption_business_data({}) == vision_adoption_business_data({"taxRate": "0"})
    assert vision_adoption_business_data({}) != vision_adoption_business_data({"taxRate": "0.06"})
