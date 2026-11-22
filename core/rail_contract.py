"""Shared seat semantics for read queries, trip decisions and quotes."""

SEAT_CLASSES = {'second_class':'二等座','first_class':'一等座','business_class':'商务座',
                'premium_class':'特等座','soft_sleeper':'软卧','hard_sleeper':'硬卧',
                'soft_seat':'软座','hard_seat':'硬座','no_seat':'无座'}
SEAT_REQUIREMENTS = {'required','preferred'}
PRICE_PREFERENCES = {'balanced','lower_price'}


def select_seat(train, request):
    """One seat decision for query filtering, trip ranking and every quoted price."""
    target=SEAT_CLASSES.get(request.get('seat_class'))
    seats=[s for s in train.get('seats',[]) if s.get('availability')=='available'
           and (not target or request.get('seat_requirement')=='preferred' or s.get('name')==target)]
    if not seats:return None
    seat=min(seats,key=lambda s:(bool(target and s.get('name')!=target),s.get('price_cny') is None,
                                s.get('price_cny') if s.get('price_cny') is not None else 0,s.get('name') or ''))
    return {**seat,'seat_class':next((key for key,name in SEAT_CLASSES.items() if name==seat.get('name')),None),
            'currency':'CNY'}


def seat_matches(train, request):
    return not request.get('seat_class') or request.get('seat_requirement')=='preferred' or select_seat(train,request) is not None
