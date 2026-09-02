#!/usr/bin/env python3
"""Reusable LC29H-BS survey-in protocol and background state manager."""

import copy
import math
import re
import threading
import time

try:
    import serial
except ImportError:  # Protocol/conversion tests do not need pyserial installed.
    serial = None


LC29H_RECEIVER_ID = "Quectel_LC29HBS"
LC29H_RECEIVER_NAME = "Quectel LC29H-BS"
ACTIVE_STATES = ("starting", "surveying", "setting_fixed")
NMEA_SENTENCE_PATTERN = re.compile(
    r"\$[A-Z][A-Z0-9]*(?:,[^$*\r\n]*)?\*[0-9A-Fa-f]{2}")


class LC29HError(Exception):
    """Base exception for survey-in failures."""


class SurveyConflictError(LC29HError):
    """Raised when a second operation would contend for the receiver."""


class SurveyStateError(LC29HError):
    """Raised when an operation is invalid for the current survey state."""


class SurveyValidationError(LC29HError):
    """Raised for invalid operator/configuration input."""


def is_lc29h_receiver(receiver):
    """Accept the identifiers written by both the existing branch and V1."""
    normalized = "".join(character for character in str(receiver).lower()
                         if character.isalnum())
    return normalized in ("lc29hbs", "quectellc29hbs")


def calculate_nmea_checksum(sentence):
    """Append the NMEA XOR checksum to a sentence beginning with ``$``."""
    if not isinstance(sentence, str) or not sentence.startswith("$"):
        raise SurveyValidationError("NMEA sentence must begin with '$'.")
    payload = sentence[1:].split("*", 1)[0]
    checksum = 0
    for character in payload:
        checksum ^= ord(character)
    return "${}*{:02X}".format(payload, checksum)


def validate_nmea_checksum(sentence):
    if "*" not in sentence:
        return False
    body, supplied = sentence.rsplit("*", 1)
    return calculate_nmea_checksum(body).rsplit("*", 1)[1] == supplied[:2].upper()


def extract_nmea_sentences(chunk):
    """Extract complete NMEA sentences from mixed RTCM and ASCII data."""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("ascii", errors="ignore")
    if not isinstance(chunk, str):
        return []
    return NMEA_SENTENCE_PATTERN.findall(chunk)


def parse_survey_status(sentence):
    """Parse one PQTMSVINSTATUS sentence into typed survey fields.

    LC29H-BS firmware emits three numeric fields after the empty field.  The
    middle one (field 6) is the observation count used by the original tool.
    """
    if not isinstance(sentence, str) or not sentence.startswith("$PQTMSVINSTATUS,"):
        raise SurveyValidationError("Not a PQTMSVINSTATUS sentence.")
    if not validate_nmea_checksum(sentence):
        raise SurveyValidationError("PQTMSVINSTATUS checksum is missing or invalid.")

    fields = sentence.split("*", 1)[0].split(",")
    if len(fields) != 12:
        raise SurveyValidationError(
            "Malformed PQTMSVINSTATUS: expected 12 fields, received {}.".format(len(fields)))
    try:
        valid_flag = int(fields[3])
        observations = int(fields[6])
        mean_x = float(fields[8])
        mean_y = float(fields[9])
        mean_z = float(fields[10])
        mean_accuracy = float(fields[11])
    except (TypeError, ValueError) as error:
        raise SurveyValidationError(
            "Malformed PQTMSVINSTATUS numeric field: {}.".format(error))

    if valid_flag not in (0, 1, 2):
        raise SurveyValidationError(
            "Unsupported PQTMSVINSTATUS valid flag: {}.".format(valid_flag))
    if not all(math.isfinite(value) for value in
               (mean_x, mean_y, mean_z, mean_accuracy)):
        raise SurveyValidationError("PQTMSVINSTATUS contains a non-finite value.")

    return {
        "valid_flag": valid_flag,
        "observations": observations,
        "mean_accuracy": mean_accuracy,
        "ecef": {"x": mean_x, "y": mean_y, "z": mean_z},
    }


def ecef_to_geodetic(x, y, z):
    """Convert WGS84 ECEF metres to latitude, longitude and ellipsoid height.

    This compact iterative conversion avoids adding pyproj to the RTKBase web
    process on ARMv6 while retaining sub-millimetre agreement for terrestrial
    coordinates.  Height is ellipsoidal; no geoid/MSL correction is applied.
    """
    x, y, z = float(x), float(y), float(z)
    if not all(math.isfinite(value) for value in (x, y, z)):
        raise SurveyValidationError("ECEF coordinates must be finite numbers.")

    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_sq = flattening * (2.0 - flattening)
    polar_radius = semi_major * (1.0 - flattening)
    horizontal = math.hypot(x, y)
    longitude = math.atan2(y, x)

    if horizontal < 1e-9:
        if abs(z) < 1e-9:
            raise SurveyValidationError("ECEF origin has no geodetic coordinate.")
        latitude = math.copysign(math.pi / 2.0, z)
        height = abs(z) - polar_radius
        return math.degrees(latitude), math.degrees(longitude), height

    latitude = math.atan2(z, horizontal * (1.0 - eccentricity_sq))
    height = 0.0
    for unused_iteration in range(12):
        sin_latitude = math.sin(latitude)
        prime_vertical = semi_major / math.sqrt(
            1.0 - eccentricity_sq * sin_latitude * sin_latitude)
        height = horizontal / math.cos(latitude) - prime_vertical
        next_latitude = math.atan2(
            z,
            horizontal * (1.0 - eccentricity_sq * prime_vertical /
                          (prime_vertical + height)),
        )
        if abs(next_latitude - latitude) < 1e-13:
            latitude = next_latitude
            break
        latitude = next_latitude

    sin_latitude = math.sin(latitude)
    prime_vertical = semi_major / math.sqrt(
        1.0 - eccentricity_sq * sin_latitude * sin_latitude)
    height = horizontal / math.cos(latitude) - prime_vertical
    return math.degrees(latitude), math.degrees(longitude), height


def format_rtkbase_position(latitude, longitude, ellipsoid_height):
    """Return the Settings-page ``position`` value without labels or units."""
    return "{:.9f} {:.9f} {:.3f}".format(
        float(latitude), float(longitude), float(ellipsoid_height))


def _serial_factory_or_error(serial_factory):
    if serial_factory is not None:
        return serial_factory
    if serial is None:
        raise LC29HError("pyserial is not installed.")
    return serial.Serial


def _open_serial(serial_factory, port, baud, timeout):
    factory = _serial_factory_or_error(serial_factory)
    try:
        return factory(port, baudrate=baud, timeout=timeout, exclusive=True)
    except TypeError:
        # Test doubles and old pyserial versions may not expose ``exclusive``.
        try:
            return factory(port, baudrate=baud, timeout=timeout)
        except Exception as error:
            raise LC29HError(
                "Could not open serial device {} at {} baud: {}".format(
                    port, baud, error))
    except Exception as error:
        raise LC29HError(
            "Could not open serial device {} at {} baud: {}".format(
                port, baud, error))


def _write_command(serial_port, command):
    sentence = calculate_nmea_checksum(command)
    serial_port.write((sentence + "\r\n").encode("ascii"))
    return sentence


def _read_command_ack(serial_port, command_name, timeout, clock=time.monotonic):
    deadline = clock() + timeout
    while clock() < deadline:
        for response in extract_nmea_sentences(serial_port.readline()):
            if not response.startswith("$" + command_name + ","):
                continue
            if not validate_nmea_checksum(response):
                raise LC29HError(
                    "Receiver returned an acknowledgement with an invalid checksum.")
            fields = response.split("*", 1)[0].split(",")
            if any(field.upper() == "OK" for field in fields[1:]):
                return response
            if any(field.upper() in ("ERROR", "FAIL", "FAILED")
                   for field in fields[1:]):
                raise LC29HError("Receiver rejected {} command: {}".format(
                    command_name, response))
    raise LC29HError("Timed out waiting for receiver acknowledgement to {}.".format(
        command_name))


def set_fixed_position(port, baud, ecef, timeout=3.0, serial_factory=None):
    serial_port = _open_serial(serial_factory, port, baud, min(1.0, timeout))
    try:
        command = "$PQTMCFGSVIN,W,2,0,0.0,{},{},{}".format(
            ecef["x"], ecef["y"], ecef["z"])
        _write_command(serial_port, command)
        return _read_command_ack(serial_port, "PQTMCFGSVIN", timeout)
    finally:
        serial_port.close()


def disable_survey(port, baud, timeout=3.0, serial_factory=None):
    serial_port = _open_serial(serial_factory, port, baud, min(1.0, timeout))
    try:
        _write_command(serial_port, "$PQTMCFGSVIN,W,0,0,0.0,0.0,0.0,0.0")
    finally:
        serial_port.close()


class LC29HSurveyManager:
    """Own one receiver and expose a lock-protected, JSON-ready state."""

    def __init__(self, before_serial=None, main_service_running=None,
                 serial_factory=None, status_timeout=15.0, clock=time.monotonic):
        self._before_serial = before_serial or (lambda unused_port: None)
        self._main_service_running = main_service_running or (lambda: False)
        self._serial_factory = serial_factory
        self._status_timeout = status_timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker = None
        self._serial_owned = False
        self._state = self._new_state()

    @staticmethod
    def _new_state():
        return {
            "state": "idle",
            "elapsed": 0,
            "remaining": None,
            "observations": None,
            "mean_accuracy": None,
            "valid_flag": None,
            "ecef": None,
            "geodetic": None,
            "rtkbase_position": None,
            "error": None,
            "port": None,
            "baud": None,
            "minimum_duration": None,
            "accuracy_limit": None,
            "main_service_running": False,
        }

    def get_status(self):
        with self._lock:
            status = copy.deepcopy(self._state)
        try:
            status["main_service_running"] = bool(self._main_service_running())
        except Exception:
            status["main_service_running"] = None
        return status

    def is_serial_busy(self):
        with self._lock:
            return self._serial_owned or self._state["state"] in ACTIVE_STATES

    def start(self, port, baud, minimum_duration, accuracy_limit):
        if isinstance(minimum_duration, bool):
            raise SurveyValidationError(
                "Minimum duration must be a whole number of seconds.")
        try:
            duration_number = float(minimum_duration)
            if not math.isfinite(duration_number) or not duration_number.is_integer():
                raise ValueError
            minimum_duration = int(duration_number)
            accuracy_limit = float(accuracy_limit)
            baud = int(baud)
        except (TypeError, ValueError, OverflowError):
            raise SurveyValidationError(
                "Minimum duration, accuracy limit and baud rate must be valid numbers.")
        if not isinstance(port, str) or not port:
            raise SurveyValidationError("The configured receiver port is empty.")
        if baud <= 0:
            raise SurveyValidationError("The configured baud rate must be positive.")
        if minimum_duration <= 0:
            raise SurveyValidationError("Minimum duration must be greater than zero.")
        if accuracy_limit <= 0 or not math.isfinite(accuracy_limit):
            raise SurveyValidationError("Accuracy limit must be a positive number.")

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise SurveyConflictError("A survey operation is already running.")
            if self._state["state"] in ACTIVE_STATES:
                raise SurveyConflictError("A survey operation is already running.")
            self._cancel.clear()
            self._state = self._new_state()
            self._state.update({
                "state": "starting",
                "port": port,
                "baud": baud,
                "minimum_duration": minimum_duration,
                "accuracy_limit": accuracy_limit,
                "remaining": minimum_duration,
            })
            self._worker = threading.Thread(
                target=self._survey_worker,
                args=(port, baud, minimum_duration, accuracy_limit),
                daemon=True,
                name="lc29h-survey",
            )
            try:
                self._worker.start()
            except Exception as error:
                self._worker = None
                self._state["state"] = "error"
                self._state["error"] = \
                    "Could not start survey background task: {}".format(error)
                raise LC29HError(self._state["error"])
            return self.get_status()

    def stop(self):
        with self._lock:
            if self._state["state"] not in ("starting", "surveying"):
                raise SurveyStateError("No survey is currently running.")
            self._cancel.set()
        return self.get_status()

    def wait(self, timeout=None):
        """Wait for the current worker; primarily used for clean CLI cancel."""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.get_status()

    def set_fixed(self):
        with self._lock:
            if self._state["state"] != "complete" or self._state["ecef"] is None:
                raise SurveyStateError(
                    "Fixed mode is only available after a completed survey.")
            port = self._state["port"]
            baud = self._state["baud"]
            ecef = copy.deepcopy(self._state["ecef"])
            self._state["state"] = "setting_fixed"
            self._state["error"] = None

        try:
            self._before_serial(port)
            acknowledgement = set_fixed_position(
                port, baud, ecef, serial_factory=self._serial_factory)
        except Exception as error:
            with self._lock:
                self._state["state"] = "complete"
                self._state["error"] = "Could not set fixed position: {}".format(error)
            raise LC29HError(self._state["error"])
        else:
            with self._lock:
                self._state["state"] = "fixed"
                self._state["error"] = None
                self._state["fixed_acknowledgement"] = acknowledgement
            return self.get_status()

    def _survey_worker(self, port, baud, minimum_duration, accuracy_limit):
        serial_port = None
        start_time = None
        last_valid_status = None
        last_parse_error = None
        try:
            self._before_serial(port)
            if self._cancel.is_set():
                with self._lock:
                    self._state["state"] = "stopped"
                    self._state["error"] = "Survey cancelled by operator."
                return

            serial_port = _open_serial(self._serial_factory, port, baud, 1.0)
            with self._lock:
                self._serial_owned = True
            if hasattr(serial_port, "reset_input_buffer"):
                serial_port.reset_input_buffer()
            _write_command(
                serial_port,
                "$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1")
            _read_command_ack(
                serial_port, "PQTMCFGMSGRATE", min(3.0, self._status_timeout),
                clock=self._clock)
            command = "$PQTMCFGSVIN,W,1,{},{},0.0,0.0,0.0".format(
                minimum_duration, accuracy_limit)
            _write_command(serial_port, command)
            _read_command_ack(
                serial_port, "PQTMCFGSVIN", min(3.0, self._status_timeout),
                clock=self._clock)
            start_time = self._clock()
            last_valid_status = start_time
            with self._lock:
                self._state["state"] = "surveying"

            while not self._cancel.is_set():
                responses = extract_nmea_sentences(serial_port.readline())
                now = self._clock()
                elapsed = max(0, int(now - start_time))
                with self._lock:
                    self._state["elapsed"] = elapsed
                    self._state["remaining"] = max(
                        0, minimum_duration - elapsed)

                for response in responses:
                    if not response.startswith("$PQTMSVINSTATUS,"):
                        continue
                    try:
                        parsed = parse_survey_status(response)
                    except SurveyValidationError as error:
                        last_parse_error = str(error)
                    else:
                        last_valid_status = now
                        last_parse_error = None
                        latitude, longitude, height = ecef_to_geodetic(
                            parsed["ecef"]["x"], parsed["ecef"]["y"],
                            parsed["ecef"]["z"])
                        geodetic = {
                            "latitude": latitude,
                            "longitude": longitude,
                            "ellipsoid_height": height,
                        }
                        with self._lock:
                            self._state.update(parsed)
                            self._state["geodetic"] = geodetic
                            self._state["rtkbase_position"] = format_rtkbase_position(
                                latitude, longitude, height)
                            if parsed["valid_flag"] == 1:
                                self._state["state"] = "surveying"
                            elif parsed["valid_flag"] == 2:
                                self._state["state"] = "complete"
                                self._state["remaining"] = 0
                                return
                            else:
                                raise LC29HError(
                                    "Receiver reports that survey-in is not active (valid_flag 0).")

                if now - last_valid_status > self._status_timeout:
                    if last_parse_error:
                        raise LC29HError(
                            "Receiver status remained malformed: {}".format(
                                last_parse_error))
                    raise LC29HError(
                        "Timed out waiting for PQTMSVINSTATUS from the receiver.")

            if serial_port is not None:
                _write_command(
                    serial_port, "$PQTMCFGSVIN,W,0,0,0.0,0.0,0.0,0.0")
            with self._lock:
                self._state["state"] = "stopped"
                self._state["error"] = "Survey cancelled by operator."
        except Exception as error:
            with self._lock:
                self._state["state"] = "error"
                self._state["error"] = str(error) or error.__class__.__name__
        finally:
            if serial_port is not None:
                try:
                    serial_port.close()
                except Exception:
                    pass
            with self._lock:
                self._serial_owned = False
