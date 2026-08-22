from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, TypeAlias, cast

import yaml  # type: ignore[import-untyped]

from ..engine.serial_actions import parse_hex_bytes
from .model import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    DeviceTargetState,
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseSpaHeatingStep,
    ExerciseStatusRetryStep,
    ExpectSerialStep,
    ObserveSleepCycleStep,
    RestoreOriginalStateStep,
    SerialSendStep,
    SetDeviceStep,
    SetSetpointStep,
    TestcaseAccess,
    TestcaseDefinition,
    TestcaseMode,
    TestcaseProtocol,
    TestcaseRequirements,
    TestcaseStep,
    TestcaseSuiteConfig,
    TestcaseSuiteDefinition,
    TestcaseSuiteMember,
    VerifyEquipmentStatusStep,
    WaitForStableEquipmentStep,
    WaitForStep,
)

_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m)$")
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_DEVICE_STATES = {
    "on",
    "off",
    "original",
    "opposite-of-original",
    "requested",
}


class TestcaseValidationError(ValueError):
    """Raised when a declarative testcase is unsafe or malformed."""


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    pass


# PyYAML defaults to YAML 1.1, where plain ``on`` and ``off`` are booleans.
# Testcases use those words as device states, so retain booleans only for the
# YAML 1.2 spellings ``true`` and ``false``.
_UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_UniqueKeyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise TestcaseValidationError(
                f"YAML mapping key {key!r} is not a scalar"
            ) from error
        if duplicate:
            raise TestcaseValidationError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)

StepParser: TypeAlias = Callable[[Mapping[str, object], str], TestcaseStep]
TestcaseDocument: TypeAlias = TestcaseDefinition | TestcaseSuiteDefinition


def load_testcase_document(path: Path) -> TestcaseDocument:
    """Load either a testcase or suite document based on its declared kind."""

    raw = _load_yaml(path)
    document = _mapping(raw, str(path))
    if document.get("kind") == "suite":
        return _parse_testcase_suite(document, path=path)
    return parse_testcase(document, source=str(path))


def load_testcase(path: Path) -> TestcaseDefinition:
    """Load and completely validate one schema-v1 YAML testcase."""

    raw = _load_yaml(path)
    return parse_testcase(raw, source=str(path))


def load_testcase_suite(path: Path) -> TestcaseSuiteDefinition:
    """Load a suite and validate its complete referenced testcase graph."""

    raw = _load_yaml(path)
    return _parse_testcase_suite(_mapping(raw, str(path)), path=path)


def _parse_testcase_suite(
    document: Mapping[str, object],
    *,
    path: Path,
) -> TestcaseSuiteDefinition:
    source = str(path)
    _keys(
        document,
        source,
        required={
            "schema",
            "kind",
            "id",
            "description",
            "mode",
            "access",
            "requires",
            "config",
            "testcases",
        },
    )
    schema = _integer(document["schema"], f"{source}.schema")
    if schema != 1:
        raise TestcaseValidationError(
            f"{source}.schema: unsupported schema {schema}; expected 1"
        )
    if _string(document["kind"], f"{source}.kind") != "suite":
        raise TestcaseValidationError(f"{source}.kind: expected 'suite'")
    identifier = _validated_identifier(document["id"], f"{source}.id")
    mode = cast(
        TestcaseMode,
        _choice(
            document["mode"],
            f"{source}.mode",
            {"physical-panel", "rs485-panel-emulator"},
        ),
    )
    access = cast(
        TestcaseAccess,
        _choice(
            document["access"],
            f"{source}.access",
            {"read-only", "read-write"},
        ),
    )
    requirements = _requirements(document["requires"], f"{source}.requires")
    config = _suite_config(document["config"], f"{source}.config")
    raw_members = document["testcases"]
    if not isinstance(raw_members, list) or not raw_members:
        raise TestcaseValidationError(f"{source}.testcases: expected a non-empty list")
    members: list[TestcaseSuiteMember] = []
    member_ids: set[str] = set()
    member_paths: set[Path] = set()
    for index, raw_member in enumerate(raw_members):
        member_path_text = _string(
            raw_member,
            f"{source}.testcases[{index}]",
        )
        member_path = (path.parent / member_path_text).resolve()
        if member_path in member_paths:
            raise TestcaseValidationError(
                f"{source}.testcases[{index}]: duplicate testcase path "
                f"{member_path_text!r}"
            )
        member_paths.add(member_path)
        testcase = load_testcase(member_path)
        if testcase.identifier in member_ids:
            raise TestcaseValidationError(
                f"{source}.testcases[{index}]: duplicate testcase id "
                f"{testcase.identifier!r}"
            )
        member_ids.add(testcase.identifier)
        if testcase.mode != mode:
            raise TestcaseValidationError(
                f"{source}.testcases[{index}]: {testcase.identifier} uses mode "
                f"{testcase.mode!r}, expected {mode!r}"
            )
        if testcase.requirements.protocol != requirements.protocol:
            raise TestcaseValidationError(
                f"{source}.testcases[{index}]: {testcase.identifier} uses "
                f"protocol {testcase.requirements.protocol!r}, expected "
                f"{requirements.protocol!r}"
            )
        if testcase.access == "read-write" and access != "read-write":
            raise TestcaseValidationError(
                f"{source}.access: read-write testcase "
                f"{testcase.identifier!r} requires read-write suite access"
            )
        members.append(TestcaseSuiteMember(member_path, testcase))
    return TestcaseSuiteDefinition(
        schema=schema,
        identifier=identifier,
        description=_string(document["description"], f"{source}.description"),
        mode=mode,
        access=access,
        requirements=requirements,
        config=config,
        members=tuple(members),
    )


def _load_yaml(path: Path) -> object:
    try:
        return yaml.load(
            path.read_text(encoding="utf-8"),
            Loader=_UniqueKeyLoader,
        )
    except OSError as error:
        raise TestcaseValidationError(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise TestcaseValidationError(f"invalid YAML in {path}: {error}") from error


def parse_testcase(raw: object, *, source: str = "<testcase>") -> TestcaseDefinition:
    document = _mapping(raw, source)
    _keys(
        document,
        source,
        required={"schema", "id", "description", "mode", "access", "requires", "steps"},
        optional={"finally"},
    )
    schema = _integer(document["schema"], f"{source}.schema")
    if schema != 1:
        raise TestcaseValidationError(
            f"{source}.schema: unsupported schema {schema}; expected 1"
        )
    identifier = _validated_identifier(document["id"], f"{source}.id")
    mode_value = _choice(
        document["mode"],
        f"{source}.mode",
        {"physical-panel", "rs485-panel-emulator"},
    )
    access_value = _choice(
        document["access"],
        f"{source}.access",
        {"read-only", "read-write"},
    )
    requirements = _requirements(document["requires"], f"{source}.requires")
    steps = _steps(document["steps"], f"{source}.steps")
    finally_steps = _steps(document.get("finally", []), f"{source}.finally")
    serial_steps = tuple(
        step for step in steps if isinstance(step, (SerialSendStep, ExpectSerialStep))
    )
    if serial_steps and requirements.protocol != "rs485":
        raise TestcaseValidationError(
            f"{source}.requires.protocol: serial steps require 'rs485'"
        )
    if requirements.protocol == "rs485" and mode_value != "rs485-panel-emulator":
        raise TestcaseValidationError(
            f"{source}.mode: rs485 protocol requires 'rs485-panel-emulator'"
        )
    if requirements.protocol == "pda" and mode_value != "physical-panel":
        raise TestcaseValidationError(
            f"{source}.mode: pda protocol requires 'physical-panel'"
        )
    if any(isinstance(step, SerialSendStep) for step in steps) and access_value != (
        "read-write"
    ):
        raise TestcaseValidationError(
            f"{source}.access: serial_send requires read-write access"
        )
    invalid_cleanup = [
        step.keyword
        for step in finally_steps
        if not isinstance(step, RestoreOriginalStateStep)
    ]
    if invalid_cleanup:
        raise TestcaseValidationError(
            f"{source}.finally: only restore_original_state is allowed"
        )
    mutates = any(
        isinstance(
            step,
            (
                SetDeviceStep,
                SetSetpointStep,
                ExerciseHeaterStep,
                VerifyEquipmentStatusStep,
                ExerciseDiscoveredDevicesStep,
                ExerciseStatusRetryStep,
                ExerciseProbeTransitionStep,
            ),
        )
        for step in steps
    )
    if mutates and access_value != "read-write":
        raise TestcaseValidationError(
            f"{source}.access: mutating steps require read-write access"
        )
    if mutates and not any(
        isinstance(step, RestoreOriginalStateStep) for step in finally_steps
    ):
        raise TestcaseValidationError(
            f"{source}.finally: mutating testcases must restore_original_state"
        )
    return TestcaseDefinition(
        schema=schema,
        identifier=identifier,
        description=_string(document["description"], f"{source}.description"),
        mode=cast(TestcaseMode, mode_value),
        access=cast(TestcaseAccess, access_value),
        requirements=requirements,
        steps=steps,
        finally_steps=finally_steps,
    )


def _requirements(raw: object, path: str) -> TestcaseRequirements:
    value = _mapping(raw, path)
    _keys(value, path, required={"protocol"})
    protocol = _choice(value["protocol"], f"{path}.protocol", {"pda", "rs485"})
    return TestcaseRequirements(protocol=cast(TestcaseProtocol, protocol))


def _suite_config(raw: object, path: str) -> TestcaseSuiteConfig:
    value = _mapping(raw, path)
    _keys(
        value,
        path,
        required=set(),
        optional={"aqualinkd_args", "overrides", "execution_role"},
    )
    raw_args = value.get("aqualinkd_args", ["-vv"])
    if not isinstance(raw_args, list):
        raise TestcaseValidationError(f"{path}.aqualinkd_args: expected a list")
    arguments = tuple(
        _string(argument, f"{path}.aqualinkd_args[{index}]")
        for index, argument in enumerate(raw_args)
    )
    unsupported = [argument for argument in arguments if argument not in {"-v", "-vv"}]
    if unsupported:
        raise TestcaseValidationError(
            f"{path}.aqualinkd_args: unsupported argument(s): " + ", ".join(unsupported)
        )
    raw_overrides = _mapping(value.get("overrides", {}), f"{path}.overrides")
    overrides: list[tuple[str, str]] = []
    for key, raw_value in raw_overrides.items():
        if not isinstance(key, str) or not _IDENTIFIER.fullmatch(key):
            raise TestcaseValidationError(
                f"{path}.overrides: invalid configuration key {key!r}"
            )
        overrides.append((key, _string(raw_value, f"{path}.overrides.{key}")))
    execution_role = _choice(
        value.get("execution_role", "single"),
        f"{path}.execution_role",
        {"single", "awake", "sleep"},
    )
    return TestcaseSuiteConfig(
        aqualinkd_args=arguments,
        overrides=tuple(overrides),
        execution_role=cast(Literal["single", "awake", "sleep"], execution_role),
    )


def _validated_identifier(raw: object, path: str) -> str:
    identifier = _string(raw, path)
    if not _IDENTIFIER.fullmatch(identifier):
        raise TestcaseValidationError(
            f"{path}: use lowercase letters, numbers, '.', '_' or '-'"
        )
    return identifier


def _steps(raw: object, path: str) -> tuple[TestcaseStep, ...]:
    if not isinstance(raw, list):
        raise TestcaseValidationError(f"{path}: expected a list")
    parsed: list[TestcaseStep] = []
    for index, raw_step in enumerate(raw):
        step_path = f"{path}[{index}]"
        step = _mapping(raw_step, step_path)
        if len(step) != 1:
            raise TestcaseValidationError(f"{step_path}: expected exactly one keyword")
        keyword, arguments = next(iter(step.items()))
        if not isinstance(keyword, str) or keyword not in _STEP_PARSERS:
            raise TestcaseValidationError(f"{step_path}: unknown keyword {keyword!r}")
        parsed.append(
            _STEP_PARSERS[keyword](
                _mapping(arguments, f"{step_path}.{keyword}"),
                f"{step_path}.{keyword}",
            )
        )
    return tuple(parsed)


def _wait_for(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"condition", "timeout"})
    return WaitForStep(
        _string(value["condition"], f"{path}.condition"),
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _serial_send(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"bytes", "timeout"})
    return SerialSendStep(
        _serial_bytes(value["bytes"], f"{path}.bytes"),
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _expect_serial(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"bytes", "timeout"})
    return ExpectSerialStep(
        _serial_bytes(value["bytes"], f"{path}.bytes"),
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _set_device(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(
        value,
        path,
        required={"id", "state", "activation_timeout", "completion_timeout"},
        optional={"convergence_timeout"},
    )
    return SetDeviceStep(
        identifier=_string(value["id"], f"{path}.id"),
        state=cast(
            DeviceTargetState,
            _choice(
                value["state"],
                f"{path}.state",
                {"on", "off", "original", "opposite-of-original"},
            ),
        ),
        activation_timeout_seconds=_duration(
            value["activation_timeout"], f"{path}.activation_timeout"
        ),
        completion_timeout_seconds=_duration(
            value["completion_timeout"], f"{path}.completion_timeout"
        ),
        convergence_timeout_seconds=_duration(
            value.get("convergence_timeout", "10s"),
            f"{path}.convergence_timeout",
        ),
    )


def _set_setpoint(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(
        value,
        path,
        required={"id", "value", "activation_timeout", "completion_timeout"},
        optional={"convergence_timeout"},
    )
    target = value["value"]
    if target != "original":
        target = _integer(target, f"{path}.value")
    return SetSetpointStep(
        identifier=_string(value["id"], f"{path}.id"),
        value=target,
        activation_timeout_seconds=_duration(
            value["activation_timeout"], f"{path}.activation_timeout"
        ),
        completion_timeout_seconds=_duration(
            value["completion_timeout"], f"{path}.completion_timeout"
        ),
        convergence_timeout_seconds=_duration(
            value.get("convergence_timeout", "10s"),
            f"{path}.convergence_timeout",
        ),
    )


def _exercise_heater(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(
        value,
        path,
        required={"id", "activation_timeout", "completion_timeout"},
        optional={"optional", "convergence_timeout"},
    )
    optional = value.get("optional", False)
    if not isinstance(optional, bool):
        raise TestcaseValidationError(f"{path}.optional: expected a boolean")
    return ExerciseHeaterStep(
        identifier=_string(value["id"], f"{path}.id"),
        optional=optional,
        activation_timeout_seconds=_duration(
            value["activation_timeout"], f"{path}.activation_timeout"
        ),
        completion_timeout_seconds=_duration(
            value["completion_timeout"], f"{path}.completion_timeout"
        ),
        convergence_timeout_seconds=_duration(
            value.get("convergence_timeout", "10s"),
            f"{path}.convergence_timeout",
        ),
    )


def _exercise_spa_heating(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return ExerciseSpaHeatingStep(_duration(value["timeout"], f"{path}.timeout"))


def _assert_device(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"id", "state", "timeout"})
    return AssertDeviceStep(
        _string(value["id"], f"{path}.id"),
        _device_state(value["state"], f"{path}.state"),
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _assert_log(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"contains", "timeout"})
    return AssertLogStep(
        _string(value["contains"], f"{path}.contains"),
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _assert_no_log(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"duration"}, optional={"contains", "level"})
    contains = _optional_string(value.get("contains"), f"{path}.contains")
    level = _optional_string(value.get("level"), f"{path}.level")
    if contains is None and level is None:
        raise TestcaseValidationError(f"{path}: either contains or level is required")
    return AssertNoLogStep(
        contains,
        level,
        _duration(value["duration"], f"{path}.duration"),
    )


def _wait_for_stable(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"devices", "timeout"})
    raw_devices = value["devices"]
    if not isinstance(raw_devices, list) or not raw_devices:
        raise TestcaseValidationError(f"{path}.devices: expected a non-empty list")
    devices = tuple(
        _string(item, f"{path}.devices[{index}]")
        for index, item in enumerate(raw_devices)
    )
    if len(set(devices)) != len(devices):
        raise TestcaseValidationError(f"{path}.devices: duplicate device id")
    return WaitForStableEquipmentStep(
        devices,
        _duration(value["timeout"], f"{path}.timeout"),
    )


def _restore(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, optional={"timeout"})
    return RestoreOriginalStateStep(
        _duration(value.get("timeout", "300s"), f"{path}.timeout")
    )


def _verify_equipment_status(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return VerifyEquipmentStatusStep(_duration(value["timeout"], f"{path}.timeout"))


def _exercise_discovered_devices(
    value: Mapping[str, object], path: str
) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return ExerciseDiscoveredDevicesStep(_duration(value["timeout"], f"{path}.timeout"))


def _observe_sleep_cycle(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return ObserveSleepCycleStep(_duration(value["timeout"], f"{path}.timeout"))


def _exercise_status_retry(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return ExerciseStatusRetryStep(_duration(value["timeout"], f"{path}.timeout"))


def _exercise_probe_transition(value: Mapping[str, object], path: str) -> TestcaseStep:
    _keys(value, path, required={"timeout"})
    return ExerciseProbeTransitionStep(_duration(value["timeout"], f"{path}.timeout"))


_STEP_PARSERS: dict[str, StepParser] = {
    "wait_for": _wait_for,
    "serial_send": _serial_send,
    "expect_serial": _expect_serial,
    "set_device": _set_device,
    "set_setpoint": _set_setpoint,
    "exercise_heater": _exercise_heater,
    "exercise_spa_heating": _exercise_spa_heating,
    "assert_device": _assert_device,
    "assert_log": _assert_log,
    "assert_no_log": _assert_no_log,
    "wait_for_stable_equipment": _wait_for_stable,
    "restore_original_state": _restore,
    "verify_equipment_status": _verify_equipment_status,
    "exercise_discovered_devices": _exercise_discovered_devices,
    "observe_sleep_cycle": _observe_sleep_cycle,
    "exercise_status_retry": _exercise_status_retry,
    "exercise_probe_transition": _exercise_probe_transition,
}


def _mapping(raw: object, path: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise TestcaseValidationError(f"{path}: expected a mapping")
    if not all(isinstance(key, str) for key in raw):
        raise TestcaseValidationError(f"{path}: keys must be strings")
    return cast(Mapping[str, object], raw)


def _keys(
    value: Mapping[str, object],
    path: str,
    *,
    required: set[str] | None = None,
    optional: set[str] | None = None,
) -> None:
    required = required or set()
    allowed = required | (optional or set())
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise TestcaseValidationError(
            f"{path}: missing required key(s): {', '.join(sorted(missing))}"
        )
    if unknown:
        raise TestcaseValidationError(
            f"{path}: unknown key(s): {', '.join(sorted(unknown))}"
        )


def _string(raw: object, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise TestcaseValidationError(f"{path}: expected a non-empty string")
    return raw.strip()


def _optional_string(raw: object, path: str) -> str | None:
    return None if raw is None else _string(raw, path)


def _serial_bytes(raw: object, path: str) -> bytes:
    value = _string(raw, path)
    try:
        return parse_hex_bytes(value)
    except ValueError as error:
        raise TestcaseValidationError(f"{path}: {error}") from error


def _integer(raw: object, path: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TestcaseValidationError(f"{path}: expected an integer")
    return raw


def _choice(raw: object, path: str, choices: set[str]) -> str:
    value = _string(raw, path)
    if value not in choices:
        raise TestcaseValidationError(
            f"{path}: expected one of {', '.join(sorted(choices))}"
        )
    return value


def _device_state(raw: object, path: str) -> DeviceTargetState:
    return cast(DeviceTargetState, _choice(raw, path, _DEVICE_STATES))


def _duration(raw: object, path: str) -> float:
    value = _string(raw, path)
    match = _DURATION.fullmatch(value)
    if match is None:
        raise TestcaseValidationError(
            f"{path}: expected a duration such as 250ms, 10s, or 3m"
        )
    seconds = float(match.group("value"))
    if match.group("unit") == "ms":
        seconds /= 1000
    elif match.group("unit") == "m":
        seconds *= 60
    if seconds <= 0:
        raise TestcaseValidationError(f"{path}: duration must be greater than zero")
    return seconds
