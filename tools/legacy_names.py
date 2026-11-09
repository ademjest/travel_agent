"""Persisted legacy names keep their canonical action identities; no execution lives here."""

LEGACY_NAMES = {name: name for name in (
    'get_current_weather', 'get_weather_forecast', 'get_driving_route', 'get_route_traffic',
    'search_travel_places', 'get_walking_route', 'get_transit_route',
    'create_reservation_draft_from_image', 'list_reservation_plans', 'refresh_reservation_plan',
    'confirm_reservation_plan', 'cancel_reservation_plan', 'complete_reservation_item_date',
    'update_reservation_draft_item', 'update_reservation_draft_items', 'add_reservation_item',
    'set_reservation_reminder_times', 'modify_reservation_item_date', 'modify_reservation_item_times',
    'cancel_reservation_item', 'read_tool_artifact', 'request_missing_input',
)}
