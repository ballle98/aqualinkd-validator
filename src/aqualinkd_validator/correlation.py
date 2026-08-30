from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def correlate_http_actions_with_serial(
    timeline_path: Path,
    serial_path: Path,
) -> dict[str, Any]:
    """Correlate HTTP equipment mutations with PDA command/response traffic."""

    timeline = _read_jsonl(timeline_path)
    packets = [record for record in _read_jsonl(serial_path) if record.get("valid")]
    actions = [
        event for event in timeline if event.get("kind") == "scenario_action_started"
    ]
    acknowledgements = [
        event
        for event in timeline
        if event.get("kind") == "scenario_http_acknowledged"
    ]
    finished = [
        event for event in timeline if event.get("kind") == "scenario_action_finished"
    ]
    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        identity = _identity(action)
        start = int(action["offset_ns"])
        next_start = (
            int(actions[index + 1]["offset_ns"])
            if index + 1 < len(actions)
            else None
        )
        acknowledged = _first_matching(acknowledgements, identity, start, next_start)
        completion = _first_matching(finished, identity, start, next_start)
        end = (
            int(completion["offset_ns"])
            if completion is not None
            else next_start
        )
        outbound_start = (
            int(acknowledged["offset_ns"])
            if acknowledged is not None
            else start
        )
        outbound = next(
            (
                packet
                for packet in packets
                if int(packet["offset_ns"]) >= outbound_start
                and (end is None or int(packet["offset_ns"]) <= end)
                and packet.get("direction") == "aqualinkd_to_panel"
                and str(packet.get("packet_type", "")).casefold()
                == "ack w/ command"
                and _command_byte(packet) not in {None, 0}
            ),
            None,
        )
        inbound = (
            next(
                (
                    packet
                    for packet in packets
                    if int(packet["offset_ns"]) > int(outbound["offset_ns"])
                    and (end is None or int(packet["offset_ns"]) <= end)
                    and packet.get("direction") == "panel_to_aqualinkd"
                ),
                None,
            )
            if outbound is not None
            else None
        )
        errors: list[str] = []
        if acknowledged is None:
            errors.append("HTTP action acknowledgement is missing")
        if outbound is None:
            errors.append("no outbound PDA Ack w/ Command packet was captured")
        if outbound is not None and inbound is None:
            errors.append("no subsequent inbound panel packet was captured")
        results.append(
            {
                "phase": action.get("phase"),
                "action": action.get("action"),
                "target": action.get("target"),
                "value": action.get("value"),
                "request_offset_ns": start,
                "http_acknowledged_offset_ns": (
                    acknowledged.get("offset_ns")
                    if acknowledged is not None
                    else None
                ),
                "outbound": _packet_evidence(outbound),
                "inbound": _packet_evidence(inbound),
                "status": "passed" if not errors else "failed",
                "errors": errors,
            }
        )

    status = (
        "not_applicable"
        if not results
        else (
            "passed"
            if all(item["status"] == "passed" for item in results)
            else "failed"
        )
    )
    return {
        "schema_version": 1,
        "status": status,
        "method": (
            "scenario HTTP acknowledgement followed by a nonzero PDA "
            "Ack w/ Command and subsequent inbound panel packet"
        ),
        "action_count": len(results),
        "passed_count": sum(item["status"] == "passed" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "actions": results,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _identity(event: dict[str, Any]) -> tuple[Any, Any, Any]:
    return event.get("phase"), event.get("action"), event.get("target")


def _first_matching(
    events: list[dict[str, Any]],
    identity: tuple[Any, Any, Any],
    start: int,
    end: int | None,
) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in events
            if _identity(event) == identity
            and int(event["offset_ns"]) >= start
            and (end is None or int(event["offset_ns"]) < end)
        ),
        None,
    )


def _command_byte(packet: dict[str, Any]) -> int | None:
    try:
        payload = bytes.fromhex(str(packet["data"]))
    except (KeyError, ValueError):
        return None
    return payload[5] if len(payload) > 5 else None


def _packet_evidence(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    if packet is None:
        return None
    return {
        "offset_ns": packet.get("offset_ns"),
        "direction": packet.get("direction"),
        "protocol": packet.get("protocol"),
        "packet_type": packet.get("packet_type"),
        "data": packet.get("data"),
        "command_byte": _command_byte(packet),
    }
