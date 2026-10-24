import json

from api import history


def entry(state, counted, is_bot=False, row_state="read"):
    """`row_state` is the row's tier before the event, as the ledger stores it."""
    return {"state": state, "counted": counted, "is_bot": is_bot, "event": "E", "channel": "whatsapp", "row_state": row_state}


def test_an_event_that_raised_the_tier_gets_no_note():
    assert history.event_note(entry("read", True, row_state="read")) == ""


def test_a_bot_flagged_event_keeps_its_own_label():
    assert history.event_note(entry("read", False, is_bot=True, row_state="delivered")) == "bot-flagged, not counted"


def test_a_failed_event_carries_no_tier_note_because_its_state_says_failed():
    assert history.event_note(entry("failed", False, row_state="delivered")) == ""
    assert history.event_note(entry("unparsed", False, row_state="delivered")) == ""


def test_an_event_below_the_rows_tier_names_the_tier_the_row_is_at():
    assert history.event_note(entry("sent", False, row_state="read")) == "row already at read"
    assert history.event_note(entry("delivered", False, row_state="acknowledged")) == "row already at acknowledged"


def test_an_event_at_the_rows_tier_reads_already_at_this_tier():
    assert history.event_note(entry("delivered", False, row_state="delivered")) == "already at this tier"


def test_an_uncounted_event_with_no_known_cause_is_not_called_a_tier_repeat():
    assert history.event_note(entry("read", False, row_state="none")) == "not counted"


def status_raw(status, errors=None):
    st = {"id": "wamid.x", "status": status, **({"errors": errors} if errors else {})}
    return {"whatsAppWebhookEntry": {"id": "1", "changes": [{"field": "messages", "value": {"statuses": [st]}}]}}


def test_the_whatsapp_status_chip_shows_the_status_value():
    assert history.whatsapp_status_chip("whatsapp", status_raw("read")) == "read"
    assert history.whatsapp_status_chip("whatsapp", status_raw("delivered")) == "delivered"


def test_a_failed_whatsapp_status_chip_carries_the_error_code():
    assert history.whatsapp_status_chip("whatsapp", status_raw("failed", [{"code": 131031}])) == "failed · error 131031"


def test_no_chip_for_other_channels_or_payloads_without_a_status():
    assert history.whatsapp_status_chip("rcs", status_raw("read")) is None
    assert history.whatsapp_status_chip("whatsapp", {"whatsAppWebhookEntry": {"changes": [{"value": {"messages": []}}]}}) is None
    assert history.whatsapp_status_chip("whatsapp", None) is None
    assert history.whatsapp_status_chip("whatsapp", {"whatsAppWebhookEntry": "not an object"}) is None
    json.dumps(history.whatsapp_status_chip("whatsapp", status_raw("read")))  # plain string, safe for the API body


def test_an_entry_written_before_the_row_tier_was_stored_gets_no_tier_note():
    old = {"state": "delivered", "counted": False, "is_bot": False}
    assert history.event_note(old) == ""
    assert history.event_note({**old, "is_bot": True}) == "bot-flagged, not counted"  # the bot label never depended on the row


def test_the_note_is_fixed_by_the_row_tier_at_event_time():
    # the same event is judged against the row as it was then, however far the row has climbed since
    assert history.event_note(entry("delivered", False, row_state="delivered")) == "already at this tier"
    assert history.event_note(entry("delivered", False, row_state="read")) == "row already at read"
