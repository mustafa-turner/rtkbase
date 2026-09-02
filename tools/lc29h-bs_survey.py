#!/usr/bin/env python3
"""Command-line interface for the reusable LC29H-BS survey module."""

import argparse
import sys
import time

import serial

from lc29h_survey import (
    LC29HError,
    LC29HSurveyManager,
    calculate_nmea_checksum,
    disable_survey,
    set_fixed_position,
)


BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 921600]


def detect_speed(port, timeout, verbose=False):
    command = calculate_nmea_checksum("$PQTMVERNO")
    for speed in BAUD_RATES:
        if verbose:
            print("Trying baud rate {}...".format(speed))
        try:
            with serial.Serial(port, baudrate=speed, timeout=timeout) as gps:
                gps.write((command + "\r\n").encode("ascii"))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    response = gps.readline().decode(
                        "ascii", errors="ignore").strip()
                    if response.startswith("$PQTMVERNO"):
                        if verbose:
                            print("Received response at {} baud: {}".format(
                                speed, response))
                        return speed
        except serial.SerialException:
            if verbose:
                print("Failed to open serial port at {} baud.".format(speed))
    raise LC29HError(
        "Failed to detect baud rate: no PQTMVERNO response was received.")


def print_survey_status(status):
    ecef = status.get("ecef")
    geodetic = status.get("geodetic")
    line = "Survey state: {} | elapsed: {}s | remaining: {}s".format(
        status["state"], status["elapsed"], status["remaining"])
    if status.get("observations") is not None:
        line += " | observations: {} | accuracy: {:.2f} m".format(
            status["observations"], status["mean_accuracy"])
    print(line)
    if ecef:
        print("ECEF: X={:.4f}, Y={:.4f}, Z={:.4f}".format(
            ecef["x"], ecef["y"], ecef["z"]))
    if geodetic:
        print("Geodetic: Lat={:.9f}, Lon={:.9f}, ellipsoid height={:.3f} m".format(
            geodetic["latitude"], geodetic["longitude"],
            geodetic["ellipsoid_height"]))


def run_survey(port, speed, timeout, minimum_duration, accuracy_limit):
    survey = LC29HSurveyManager(status_timeout=max(15.0, timeout * 3.0))
    survey.start(port, speed, minimum_duration, accuracy_limit)
    previous = None
    try:
        while True:
            status = survey.get_status()
            current = (status["state"], status["observations"],
                       status["mean_accuracy"])
            if current != previous:
                print_survey_status(status)
                previous = current
            if status["state"] not in ("starting", "surveying"):
                if status["state"] == "error":
                    raise LC29HError(status["error"])
                return status
            time.sleep(1)
    except KeyboardInterrupt:
        if survey.is_serial_busy():
            survey.stop()
            survey.wait(timeout=max(2.0, timeout + 1.0))
        print("Survey cancelled.")
        return survey.get_status()


def main():
    parser = argparse.ArgumentParser(
        description="Survey-in and fixed-mode tool for Quectel LC29H-BS.")
    parser.add_argument("port", help="Serial port (for example /dev/ttyGNSS)")
    parser.add_argument("--timeout", type=int, default=3,
                        help="Receiver response timeout in seconds (default: 3)")
    parser.add_argument("--speed", type=int,
                        help="Baud rate; auto-detected when omitted")
    parser.add_argument("--mode", choices=("survey", "fixed", "disable"),
                        required=True)
    parser.add_argument("--ecef", nargs=3, type=float,
                        help="ECEF X Y Z metres for fixed mode")
    parser.add_argument("--min-dur", type=int, default=600,
                        help="Minimum survey duration in seconds (default: 600)")
    parser.add_argument("--acc-limit", type=float, default=15.0,
                        help="Survey accuracy limit in metres (default: 15.0)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        speed = args.speed or detect_speed(
            args.port, args.timeout, args.verbose)
        if args.speed is None:
            print("Detected speed: {} baud".format(speed))

        if args.mode == "survey":
            status = run_survey(args.port, speed, args.timeout,
                                args.min_dur, args.acc_limit)
            if status["state"] == "complete":
                print("Survey-in complete.")
                print("RTKBase position: {}".format(
                    status["rtkbase_position"]))
        elif args.mode == "fixed":
            if not args.ecef:
                parser.error("--ecef X Y Z is required in fixed mode")
            acknowledgement = set_fixed_position(
                args.port, speed,
                {"x": args.ecef[0], "y": args.ecef[1], "z": args.ecef[2]},
                timeout=args.timeout)
            print("Fixed mode set successfully: {}".format(acknowledgement))
        else:
            disable_survey(args.port, speed, timeout=args.timeout)
            print("Survey-in disabled.")
    except (LC29HError, serial.SerialException, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
