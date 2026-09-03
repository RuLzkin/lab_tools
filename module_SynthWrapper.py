from pathlib import Path
from typing import Optional, Tuple
import time
import logging
import serial.tools.list_ports
import pickle
from tqdm.contrib import tenumerate
from windfreak import SynthHD

MSG = "SynthHDWrapper>>"
MODULE_DIR = Path(__file__).parent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Synth_Wrapper():

    list_lim_freq = []
    list_lim_pow = []

    def __init__(self, str_port: Optional[str] = None, debug: bool = False) -> None:
        """Synth_Wrapper(str_port: str = None, num_port: int = 0, debug=False)

        Args:
            str_port: str, this has priority over num_port (ex. "COM3")
            debug: debug mode (dummy mode)

        Returns:
            Synth_Wrapper: instance

        Examples:
            >>> synth = Synth_Wrapper("COM3")
            >>> synth.on(15, 18e9)
            >>> time.sleep(5.0)
            >>> synth.off()
        """
        self.debug = debug
        if debug:
            return
        if str_port is None:
            list_port = list(serial.tools.list_ports.comports())
            for _port in list_port:
                try:
                    _synth = SynthHD(_port.name)
                except:  # noqa
                    logger.info(f"{MSG} Connect -- {_port.name} >> Could not be connected")
                else:
                    logger.info(f"{MSG} Connect -- {_port.name} >> Successfully connected")
                    _synth.close()
                    str_port = _port.name
                    break
            if str_port is None:
                raise ValueError("Valid port not found")
        self.synth = SynthHD(str_port)
        for _channel in [0, 1]:
            self.synth[_channel].enable = False
            self.synth[_channel].power = -20.0
            self.synth[_channel].frequency = 8.0e9
            self.synth[_channel].phase = 0.0
        logger.info(f"{MSG} Connect to {str_port}")
        try:
            self.input_limit()
        except FileNotFoundError:
            logger.warning(f"{MSG} limit list not found")
            self.measure_limit([float(_i) * 1e9 for _i in range(5, 25, 1)])
            self.output_limit()
        else:
            logger.info(f"{MSG} load limit list")

    def on_no_feedback(
            self,
            power: Optional[float] = None,
            frequency: Optional[float] = None,
            phase: Optional[float] = None,
            channel: Optional[int] = 0) -> Tuple[float, float, bool, float]:
        """on(power: float | None, frequency: float | None, phase: float | None, channel: int | None)

        If input args are omitted, the previous values are applied

        Args:
            power (float, None): [dBm] -40 ~ 0.01 ~ +18
            frequency (float, None): [Hz] 0.01e9 ~ 0.1 ~ 24e9
            phase (float, None): [deg]
            channel (int, None): channel number

        Returns:
            self.status() (power, frequency, calibrated, phase)

        Examples:
            >>> synth.on()
            >>> synth.on(15, 16e9)
            >>> synth.on(frequency=7.5e9)
        """
        if self.debug:
            return self.status(channel=channel)
        if power is not None:
            self.synth[channel].power = float(power)
        if frequency is not None:
            self.synth[channel].frequency = float(frequency)
        self.synth[channel].enable = True
        if phase is not None:
            self.synth[channel].phase = float(phase)
        t0 = time.time()
        while not self.synth[channel].lock_status:
            if time.time() - t0 > 0.5:
                logger.warning(f"{MSG} SynthHD PLL lock timeout")
            time.sleep(0.005)
        return self.status(channel=channel)

    def off(self, channel: Optional[int] = 0) -> None:
        """off()

        that's it

        Args:
            channel (Optional[int]) (default: 0)
                if None, both 0 and 1

        Returns:
            None

        Examples:
            >>> synth.off()

        """
        if self.debug:
            return
        if channel is None:
            self.synth[0].enable = False
            self.synth[1].enable = False
        else:
            self.synth[channel].enable = False

    def status(self, flag_ghz=False, channel: Optional[int] = 0) -> Tuple[float, float, bool, float]:
        """status()

        return the current power and frequency.
        it works whether the device is switched ON or OFF

        Args:
            flag_ghz (bool, Optional): the unit of the output frequency. False=Hz, True=GHz
            channel (Optional[int]) (default: 0)
                if None, both 0 and 1

        Returns:
            power (float): [dBm]
            frequency (float): [Hz] or [GHz] (default: [Hz])
            calibrated (float): True(OK) or False(NG)
            phase (float): [deg]

        Examples:
            >>> synth.status()
        """
        if self.debug:
            return 0, 10e9, True, 0
        _pow = self.synth[channel].power
        _freq = self.synth[channel].frequency
        _cali = self.synth[channel].calibrated
        _pha = self.synth[channel].phase
        if flag_ghz:
            _freq = _freq * 1e-9
        return _pow, _freq, _cali, _pha

    def show_status(self):
        _temp = self.synth.temperature
        _ena_0 = self.synth[0].enable
        _ena_1 = self.synth[1].enable
        _sta_0 = self.status(flag_ghz=True, channel=0)
        _sta_1 = self.status(flag_ghz=True, channel=1)

        logger.info(f"{MSG} SynthHD Status")
        logger.info(f"{MSG}  * Model: {self.synth.model}")
        logger.info(f"{MSG}  * Model Type: {self.synth.model_type}")
        logger.info(f"{MSG}  * Serial Number: {self.synth.serial_number}")
        logger.info(f"{MSG}  * Firmware Version: {self.synth.firmware_version}")
        logger.info(f"{MSG}  * Hardware Version: {self.synth.hardware_version}")
        logger.info(f"{MSG}  * Reference Mode: {self.synth.reference_mode}")
        logger.info(f"{MSG}  * Reference Frequency: {self.synth.reference_frequency} Hz")
        logger.info(f"{MSG}  * Trigger Mode: {self.synth.trigger_mode}")
        logger.info(f"{MSG}  * Device Temperature: {_temp:.2f} °C")
        logger.info(f"{MSG}  * Channel 0 - ON: {_ena_0}, Pow: {_sta_0[0]:6.2f} dBm, Freq: {_sta_0[1]:6.2f} GHz, Level: {_sta_0[2]}, Phase: {_sta_0[3]:7.2f} deg")
        logger.info(f"{MSG}  * Channel 1 - ON: {_ena_1}, Pow: {_sta_1[0]:6.2f} dBm, Freq: {_sta_1[1]:6.2f} GHz, Level: {_sta_1[2]}, Phase: {_sta_1[3]:7.2f} deg")

    def on(self,
           power: Optional[float] = None,
           frequency: Optional[float] = None,
           phase: Optional[float] = None,
           use_limit: bool = True,
           channel: Optional[int] = 0) -> Tuple[float, float, bool, float]:
        """on(power: float | None, frequency: float | None)

        If input args are omitted, the previous values are applied

        Args:
            power (float, None): [dBm] -40 ~ 0.01 ~ +18
            frequency (float, None): [Hz] 0.01e9 ~ 0.1 ~ 24e9
            phase (float, None): [deg]
            use_limit (bool)
            channel (int, None): channel number

        Returns:
            self.status() (power, frequency, calibrated, phase)

        Examples:
            >>> synth.on()
            >>> synth.on(15, 16e9)
            >>> synth.on(frequency=7.5e9)
        """
        if self.debug:
            return self.status(channel=channel)
        # frequency
        if frequency is not None:
            self.synth[channel].frequency = float(frequency)
        # power
        if power is None:
            power = self.synth[channel].power
        else:
            if use_limit and frequency is not None:
                try:
                    pow_lim = self.limited_power(frequency)
                    # print("Power limit:", pow_lim)
                except ValueError:
                    # print("Power limit not found")
                    pow_lim = 15
                power = min(power, pow_lim)
            self.synth[channel].power = float(power)
        # on
        if not self.synth[channel].enable:
            self.synth[channel].enable = True
        t0 = time.time()
        while not self.synth[channel].lock_status:
            if time.time() - t0 > 0.5:
                logger.warning(f"{MSG} SynthHD PLL lock timeout")
            time.sleep(0.005)
        # phase (after enable)
        if phase is not None:
            self.synth[channel].phase = float(phase)

        is_calibrated = self.synth[channel].calibrated
        step_power = 0.1
        if power is None:
            raise ValueError("Power value is invalid")
        _sign = (power >= 0) * 2 - 1
        while not is_calibrated:
            self.synth[channel].power -= step_power * _sign
            # print("not calibrated:", self.synth[channel].power)
            time.sleep(0.005)  # for signal rise time
            is_calibrated = self.synth[channel].calibrated
        # time.sleep(0.01)  # for signal rise time
        return self.status(channel=channel)

    def output_limit(self):
        with open(MODULE_DIR / "lib_synthhd_power.wf", "wb") as f:
            pickle.dump([self.list_lim_freq, self.list_lim_pow], f)

    def input_limit(self):
        with open(MODULE_DIR / "lib_synthhd_power.wf", "rb") as f:
            _list_load = pickle.load(f)
        self.list_lim_freq, self.list_lim_pow = _list_load

    def measure_limit(self, list_hz_freq: list, channel: Optional[int] = 0):
        """measure_limit
        """
        self.list_lim_freq = []
        self.list_lim_pow = []
        for _i, _freq in tenumerate(list_hz_freq):
            _pow, _, _, _ = self.on(15, _freq, use_limit=False, channel=channel)
            time.sleep(0.1)
            self.off()
            self.list_lim_freq.append(_freq)
            self.list_lim_pow.append(_pow)

    def limited_power(self, frequency: float, verbose=False):
        if verbose:
            logger.info(f"{MSG} limiter>>> input freq: {frequency}")
        if len(self.list_lim_freq) == 0:
            raise ValueError("Invalid limit list")
        if frequency > self.list_lim_freq[-1] or frequency < self.list_lim_freq[0]:
            raise ValueError(f"{frequency} is out of list")
        try:
            index_exact = self.list_lim_freq.index(frequency)
            pow_lim = self.list_lim_pow[index_exact]
            return pow_lim
        except ValueError:
            pass

        for _i in range(len(self.list_lim_freq) - 1):
            _a, _b = self.list_lim_freq[_i], self.list_lim_freq[_i + 1]
            if _a < frequency < _b:
                pow_lim = ((frequency - _a) * self.list_lim_pow[_i] + (_b - frequency) * self.list_lim_pow[_i + 1]) / (_b - _a)
                return pow_lim

        raise ValueError("Unexpected error occured")


if __name__ == "__main__":
    synth = Synth_Wrapper("COM3")
    status = synth.on(0, 8e9, 0, channel=0)
    synth.off(channel=0)

    synth.show_status()

    """Limiter"""
    # import numpy as np
    # vec_freq = np.arange(4.5, 24.001, 0.1) * 1e9
    # synth.measure_limit(vec_freq.tolist())
    # synth.output_limit()

    """Limiter Check"""
    import numpy as np
    from matplotlib import pyplot as plt
    plt.figure()
    plt.plot(np.array(synth.list_lim_freq) * 1e-9, synth.list_lim_pow)
    plt.show()
