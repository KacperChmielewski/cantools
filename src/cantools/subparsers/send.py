import argparse
import re
from typing import Any

import can.cli

from cantools.database import EncodeError, Message, Signal

from .. import database
from ..database import Database
from ..database.namedsignalvalue import NamedSignalValue
from .__utils__ import format_message

TOLERANCE = 0.10

a = [
    "RT_SB_Trig_Forward_Dist(1, 2)",
    "RT_SB_Trig_Forward_Dist(1,2)",
    'RT_SB_Trig_Forward_Dist(1 2)',
    'RT_SB_Trig_Forward_Dist 1 2',
    'RT_SB_Trig_Forward_Dist Forward_Distance=1  Deviation_Distance=2',
    'RT_SB_Trig_Forward_Dist(Forward_Distance=1, Deviation_Distance=2)',
    'RT_SB_Trig_Forward_Dist(Forward_Distance=1,Deviation_Distance=2)',
    'RT_SB_Trig_Forward_Dist(Deviation_Distance=2,Forward_Distance=1)',
    'RT_SB_Trig_Forward_Dist(1, 2, 3, 4)',
    'RT_SB_Trig_Forward_Dist(1, 2, 3, test)',
]


def split_name_args(s: str) -> tuple[str, str]:
    s = s.strip()
    m = re.match(r'^([A-Za-z0-9_]+)\s*(?:\((.*)\)|\s+(.*))?$', s)
    if not m:
        return s, ""
    name = m.group(1)
    args = m.group(2) if m.group(2) is not None else m.group(3)
    return name, (args or "").strip()


def normalize_args(arg_str: str) -> str:
    # Replace commas with spaces
    arg_str = arg_str.replace(',', ' ')
    # Collapse multiple spaces
    arg_str = re.sub(r'\s+', ' ', arg_str)
    return arg_str.strip()


def parse_args(arg_str: str) -> tuple[list[Any], dict[Any, Any]]:
    if not arg_str:
        return [], {}

    tokens = normalize_args(arg_str).split(' ')

    positional = []
    named = {}

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if '=' in token:
            key, val = token.split('=', 1)
            named[key] = val
        # Handle "key = value" split across tokens
        elif i + 2 < len(tokens) and tokens[i + 1] == '=':
            key = token
            val = tokens[i + 2]
            named[key] = val
            i += 2
        else:
            positional.append(token)

        i += 1

    return positional, named


def parse_string(s):
    name, arg_str = split_name_args(s)
    positional, named = parse_args(arg_str)

    return {
        "name": name,
        "positional": positional,
        "named": named
    }


def get_string_of_a_signal_desc(signal: Signal) -> str:
    lines = [f"- {signal.name}:", f"   step: {signal.scale}"]
    if signal.minimum is not None:
        lines.append(f"    min: {signal.minimum}")
    if signal.maximum is not None:
        lines.append(f"    max: {signal.maximum}")
    if signal.unit is not None:
        lines.append(f"   unit: {signal.unit}")
    if signal.conversion.choices is not None:
        lines.append(f"choices:")
        for key, choice in signal.conversion.choices.items():
            lines.append(f"{key:8d}: {choice}")
    return '\n'.join(lines)


def get_signal_and_range(msg: Message) -> str:
    lines = [f"{msg.name}[0x{msg.frame_id:x}]"]
    for signal in msg.signals:
        lines.append(get_string_of_a_signal_desc(signal))

    return '\n'.join(lines)


def string_to_can_frame(dbase: Database, msg_str: str) -> tuple[Message, dict[Any, float]]:
    r = parse_string(msg_str)
    frame_name = r['name']
    positional_args = r['positional']
    named_args = r['named']

    try:
        message_from_db = dbase.get_message_by_name(frame_name)
    except KeyError as err:
        print(f"Could not find message: {err}")
        # invalid frame name. but maybe it's somewhere close?
        message_names = [message.name for message in dbase.messages]
        matching = [s for s in message_names if frame_name in s]
        if matching:
            head_line = "Did you mean:"
            candidates = matching
        else:
            head_line = "Available frame names:"
            candidates = message_names
        candidates.sort()
        raise ValueError("Invalid frame name.\n" + head_line + '\n  ' + '\n  '.join(candidates)) from None

    if len(positional_args) != 0 and len(named_args) != 0:
        raise ValueError("You cannot use named and positional arguments at the same time")

    if len(positional_args) > 0:
        for i in range(len(positional_args)):
            try:
                named_args[message_from_db.signals[i].name] = positional_args[i]
            except IndexError:
                raise ValueError(
                    f"Too much positional arguments! "
                    f"Got {len(positional_args)}, expected {len(named_args)}.") from None

    # resolve choices
    for signal_name, signal_value in named_args.items():
        signal = message_from_db.get_signal_by_name(signal_name)
        if signal.conversion.choices is not None:
            resolved_value = next((k for k, v in signal.conversion.choices.items() if v == signal_value), None)
            if resolved_value is not None:
                signal_value = resolved_value
            try:
                float(signal_value)
            except ValueError as err:
                raise ValueError(
                    f"Invalid choice value ({signal_value}) for:\n" + get_string_of_a_signal_desc(signal)) from err
        named_args[signal_name] = signal_value

    # cast values to floats
    named_args = {k: float(v) for k, v in named_args.items()}
    return message_from_db, named_args


def validate_args_tolerances(params_provided: dict, params_calculated: dict, scales: dict, tolerances: dict) -> None:
    # Check same keys first
    if set(params_provided.keys()) != set(params_calculated.keys()):
        raise KeyError("Mismatch in keys")

    if isinstance(tolerances, float):
        tolerances = dict.fromkeys(scales, tolerances)

    for key, _ in params_provided.items():
        if key not in params_calculated:
            raise KeyError(f"Missing key in b: {key}")
        value_provided = float(params_provided[key])
        value_calculated = params_calculated[key]

        if isinstance(value_calculated, NamedSignalValue):
            value_calculated = value_calculated.value
        else:
            value_calculated = float(value_calculated)

        scale = scales[key]
        tolerance = tolerances[key]

        abs_tolerance = tolerance * scale
        error = abs(value_provided - value_calculated)

        if error > abs_tolerance:
            pct_error = error / scale * 100
            raise ValueError(f"Values for {key} are not exactly the same (considering tolerances).\n"
                             f""
                             f"     param : {key}\n"
                             f"  provided : {value_provided}\n"
                             f"after calc : {value_calculated}\n"
                             f"     scale : {scale}\n"
                             f" tolerance : {tolerance * 100} %\n"
                             f"  abs tol. : {abs_tolerance}\n"
                             f"      err. : {pct_error:.2f} %\n"
                             f"  abs err. : {error}\n"
                             f"")


def _do_send(args):
    dbase = database.load_file(args.database)

    is_strict = not args.no_strict
    # send(dbase, "RT_SB_Trig_Forward_Dist asd asd")
    msg_from_db, msg_args_provided = string_to_can_frame(dbase, args.user_input)

    try:
        frame_payload = dbase.encode_message(msg_from_db.frame_id, msg_args_provided, strict=is_strict)
    except EncodeError as err:
        print(get_signal_and_range(msg_from_db))
        raise err

    msg = can.Message(arbitration_id=msg_from_db.frame_id, data=frame_payload,
                      is_extended_id=msg_from_db.is_extended_frame, is_fd=msg_from_db.is_fd)

    args_to_send = dbase.decode_message(msg.arbitration_id, msg.data)
    if not args.ignore_rounding_errors:
        scales = {s.name: s.scale for s in msg_from_db.signals}
        validate_args_tolerances(msg_args_provided, args_to_send, scales, TOLERANCE)

    formatted_message_str = format_message(msg_from_db, args_to_send, args.single_line)
    print(formatted_message_str[1:])  # 1: is here to remove leading \n

    if args.dry_run is not True:
        print("Sending")
        with can.cli.create_bus_from_namespace(args) as bus:
            bus.send(msg)
    else:
        print("We don't send, it's dry run.")
        print(msg)


def add_subparser(subparsers):
    send_parser = subparsers.add_parser(
        'send',
        description='Send CAN bus traffic in a text based user interface.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    send_parser.add_argument(
        '-d', '--database',
        required=True,
        help='Database file.')
    send_parser.add_argument(
        '-s', '--single-line',
        action='store_true',
        help='Print the decoded message on a single line.')
    send_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help="Validate user input, display can message, don't send message on the bus.")
    send_parser.add_argument(
        '-r', '--ignore-rounding-errors',
        action='store_true',
        help=f"Program checks if provided values are within {TOLERANCE * 100:.1f} percent of the signal's scale value "
             f"tolerance. You can ignore this validation.")
    send_parser.add_argument(
        '--no-strict',
        action='store_true',
        help="Don't check if provided values are within min and max.")
    send_parser.add_argument(
        'user_input',
        help='FrameName(arg1, arg2 ...)')

    can.cli.add_bus_arguments(send_parser, group_title="bus arguments (python-can)")

    send_parser.set_defaults(func=_do_send)
