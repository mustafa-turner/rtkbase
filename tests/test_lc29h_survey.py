import os
import sys
import threading
import time
import unittest


TOOLS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, TOOLS_PATH)

from lc29h_survey import (  # noqa: E402
    LC29HSurveyManager,
    SurveyConflictError,
    SurveyStateError,
    calculate_nmea_checksum,
    ecef_to_geodetic,
    extract_nmea_sentences,
    format_rtkbase_position,
    parse_survey_status,
)


def status_sentence(valid_flag):
    return calculate_nmea_checksum(
        "$PQTMSVINSTATUS,1,2,{},,5,434,7,-1841232.9690,"
        "6069117.4590,-673507.2610,1.42".format(valid_flag))


class LC29HProtocolTests(unittest.TestCase):
    def test_nmea_checksum(self):
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
        self.assertEqual(calculate_nmea_checksum(sentence), sentence + "*47")

    def test_parse_in_progress_status(self):
        parsed = parse_survey_status(status_sentence(1))
        self.assertEqual(parsed["valid_flag"], 1)
        self.assertEqual(parsed["observations"], 434)
        self.assertAlmostEqual(parsed["mean_accuracy"], 1.42)
        self.assertAlmostEqual(parsed["ecef"]["x"], -1841232.969)

    def test_parse_completed_status(self):
        parsed = parse_survey_status(status_sentence(2))
        self.assertEqual(parsed["valid_flag"], 2)
        self.assertEqual(parsed["ecef"]["z"], -673507.261)

    def test_extracts_status_with_binary_rtcm_prefix(self):
        sentence = status_sentence(1)
        chunk = b"\xd3\x00\x13\x07\x00\x02" + sentence.encode("ascii") + b"\r\n"
        self.assertEqual(extract_nmea_sentences(chunk), [sentence])

    def test_ecef_to_wgs84_geodetic(self):
        latitude, longitude, ellipsoid_height = ecef_to_geodetic(
            6378137.0, 0.0, 0.0)
        self.assertAlmostEqual(latitude, 0.0, places=10)
        self.assertAlmostEqual(longitude, 0.0, places=10)
        self.assertAlmostEqual(ellipsoid_height, 0.0, places=6)

    def test_rtkbase_copy_string_precision(self):
        self.assertEqual(
            format_rtkbase_position(-6.102266361, 106.876610694, 28.4651),
            "-6.102266361 106.876610694 28.465",
        )


class LC29HManagerSafetyTests(unittest.TestCase):
    def test_rejects_fixed_mode_before_completed_survey(self):
        manager = LC29HSurveyManager()
        with self.assertRaises(SurveyStateError):
            manager.set_fixed()

    def test_rejects_two_simultaneous_survey_starts(self):
        release_worker = threading.Event()
        manager = LC29HSurveyManager(
            before_serial=lambda unused_port: release_worker.wait())
        try:
            manager.start("/dev/ttyGNSS", 921600, 600, 15.0)
            with self.assertRaises(SurveyConflictError):
                manager.start("/dev/ttyGNSS", 921600, 600, 15.0)
        finally:
            manager.stop()
            release_worker.set()

    def test_completed_survey_can_set_final_ecef_as_fixed(self):
        class FakeSerial:
            def __init__(self, responses, writes):
                self.responses = list(responses)
                self.writes = writes

            def reset_input_buffer(self):
                pass

            def write(self, value):
                self.writes.append(value.decode("ascii"))

            def readline(self):
                if self.responses:
                    response = self.responses.pop(0)
                    if isinstance(response, bytes):
                        return response
                    return (response + "\r\n").encode("ascii")
                return b""

            def close(self):
                pass

        writes = []
        response_sets = [
            [
                b"\xd3\x00\x01" + calculate_nmea_checksum(
                    "$PQTMCFGMSGRATE,OK").encode("ascii") + b"\r\n",
                b"\xd3\x00\x02" + calculate_nmea_checksum(
                    "$PQTMCFGSVIN,OK").encode("ascii") + b"\r\n",
                b"\xd3\x00\x03" + status_sentence(1).encode("ascii") + b"\r\n",
                b"\xd3\x00\x04" + status_sentence(2).encode("ascii") + b"\r\n",
            ],
            [calculate_nmea_checksum("$PQTMCFGSVIN,OK")],
        ]

        def serial_factory(unused_port, **unused_options):
            return FakeSerial(response_sets.pop(0), writes)

        manager = LC29HSurveyManager(serial_factory=serial_factory)
        manager.start("/dev/ttyGNSS", 921600, 600, 15.0)
        deadline = time.monotonic() + 1.0
        while manager.get_status()["state"] != "complete" and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(manager.get_status()["state"], "complete")
        self.assertTrue(writes[0].startswith(
            "$PQTMCFGMSGRATE,W,PQTMSVINSTATUS,1,1"))

        status = manager.set_fixed()
        self.assertEqual(status["state"], "fixed")
        self.assertIn(
            "$PQTMCFGSVIN,W,2,0,0.0,-1841232.969,6069117.459,-673507.261",
            writes[-1],
        )


if __name__ == "__main__":
    unittest.main()
