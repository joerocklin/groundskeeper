"""Pure Store validation (spec §14.7, #31): section checks run on load."""

from custom_components.mowing_director.store import empty_store_data
from custom_components.mowing_director.store_validation import validate_store


def test_a_fresh_store_is_valid_and_untouched() -> None:
    result = validate_store(empty_store_data())
    assert (result.config_ok, result.state_ok, result.dropped) == (True, True, [])
    assert result.data == empty_store_data()


def test_missing_sections_are_tolerated_as_empty() -> None:
    result = validate_store({"config": {"recurrences": []}})
    assert result.config_ok and result.state_ok


def test_non_dict_file_content_is_an_invalid_config() -> None:
    assert not validate_store(["not", "a", "store"]).config_ok


def test_config_with_wrongly_typed_zones_is_invalid() -> None:
    data = empty_store_data()
    data["config"]["zones"] = "front lawn"
    assert not validate_store(data).config_ok


def test_config_with_a_zone_missing_its_id_is_invalid() -> None:
    data = empty_store_data()
    data["config"]["zones"] = [{"name": "front lawn"}]
    assert not validate_store(data).config_ok


def test_config_not_a_dict_is_invalid() -> None:
    data = empty_store_data()
    data["config"] = None
    assert not validate_store(data).config_ok


def test_invalid_state_is_emptied_and_config_is_kept() -> None:
    data = empty_store_data()
    data["config"]["zones"] = [{"id": "z1", "name": "front lawn"}]
    data["state"] = {"runs": "garbage"}
    result = validate_store(data)
    assert result.config_ok and not result.state_ok
    assert result.data["state"] == {}
    assert result.data["config"]["zones"] == [{"id": "z1", "name": "front lawn"}]


def test_a_run_without_status_makes_state_invalid() -> None:
    data = empty_store_data()
    data["state"] = {"runs": [{"id": "r1"}]}
    assert not validate_store(data).state_ok


def test_a_bad_history_run_is_dropped_and_reported_and_good_ones_kept() -> None:
    data = empty_store_data()
    good = {"id": "r1", "plan_id": "p1", "status": "done"}
    data["history"] = {"runs": [good, {"id": "r2"}, "junk"], "plans": {}}
    result = validate_store(data)
    assert result.state_ok and result.config_ok
    assert result.data["history"]["runs"] == [good]
    assert len(result.dropped) == 2


def test_a_bad_history_plan_is_dropped() -> None:
    data = empty_store_data()
    data["history"] = {"runs": [], "plans": {"p1": {"id": "p1"}, "p2": 7}}
    result = validate_store(data)
    assert result.data["history"]["plans"] == {"p1": {"id": "p1"}}
    assert len(result.dropped) == 1


def test_a_bad_learned_duration_sample_is_dropped() -> None:
    data = empty_store_data()
    ok = {"value": 1200.0, "native_ref": "a"}
    data["learned"] = {"durations": {"z1": [ok, {"value": "slow"}, 5]}}
    result = validate_store(data)
    assert result.data["learned"]["durations"] == {"z1": [ok]}
    assert len(result.dropped) == 2


def test_a_history_section_of_the_wrong_type_is_dropped_without_failing_the_store() -> None:
    data = empty_store_data()
    data["history"] = "oops"
    result = validate_store(data)
    assert result.config_ok and result.state_ok
    assert result.data["history"] == {}
    assert len(result.dropped) == 1
