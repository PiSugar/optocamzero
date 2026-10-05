import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from whisplay_adapter import WhisplayPWMProxy, binary_backlight_level  # noqa: E402


class WhisplayBacklightTests(unittest.TestCase):
    def test_binary_backlight_level(self):
        self.assertEqual(binary_backlight_level(0), 0)
        self.assertEqual(binary_backlight_level(0.1), 100)
        self.assertEqual(binary_backlight_level(8), 100)
        self.assertEqual(binary_backlight_level(100), 100)

    def test_pwm_proxy_never_sends_intermediate_brightness(self):
        levels = []

        class Backend:
            def set_backlight(self, level):
                levels.append(level)

        pwm = WhisplayPWMProxy(Backend())
        pwm.set_PWM_dutycycle(18, 0)
        pwm.set_PWM_dutycycle(18, 1)
        pwm.set_PWM_dutycycle(18, 128)
        pwm.set_PWM_dutycycle(18, 255)
        self.assertEqual(levels, [0, 100, 100, 100])


if __name__ == "__main__":
    unittest.main()
